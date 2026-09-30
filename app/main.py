"""展馆多语展签审校系统 — FastAPI 主应用。

角色：
  librarian  馆员：维护中文原文与语言，发布语言快照
  translator 译者：提交译文、可校对他人译文
  reviewer   校对：确认译文、也可提交译文
任何译者/校对账号均不得确认自己提交的译文。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db
from .db import get_conn, get_tx_conn, hash_password, now

# ---------------------------- 请求模型 ----------------------------


class LoginBody(BaseModel):
    username: str
    password: str


class LabelBody(BaseModel):
    source_text: str = Field(min_length=1)
    source_rev: int  # 读取时的原文修订号（乐观锁）


class LanguageBody(BaseModel):
    code: str = Field(min_length=1, max_length=10)
    name: str = Field(min_length=1, max_length=80)


class TranslationBody(BaseModel):
    text: str = Field(min_length=1)
    source_rev: int  # 译者读取时看到的原文修订号
    trans_rev: Optional[int] = None  # 修改既有译文时须携带，新建留空


class ConfirmBody(BaseModel):
    source_rev: int
    trans_rev: int


class WithdrawBody(BaseModel):
    status_rev: int  # 读取时的状态版本（乐观锁）
    reason: str = Field(min_length=1)


class RestoreBody(BaseModel):
    status_rev: int  # 读取时的状态版本（乐观锁）


class ClaimBody(BaseModel):
    source_rev: int  # 读取时的原文修订号
    trans_rev: int  # 读取时的译文修订号


class BatchPublishBody(BaseModel):
    # 至少两种不同目标语言；重复编号由接口层拒绝（409）
    language_ids: list[int] = Field(min_length=2)


class BatchCommitBody(BaseModel):
    credential: str = Field(min_length=1)  # 预览接口签发的校验凭证


# 认领有效期：默认 30 分钟，可由环境变量覆盖（仅便于测试，文档中说明）
CLAIM_TTL_SECONDS = int(os.environ.get("CLAIM_TTL_SECONDS", "1800"))
TS_FORMAT = "%Y-%m-%d %H:%M:%S"


# ---------------------------- 待审认领辅助 ----------------------------


def _parse_ts(value: str) -> datetime:
    return datetime.strptime(value, TS_FORMAT)


def _invalidate_claims(conn, *, label_id: Optional[int] = None,
                       language_id: Optional[int] = None,
                       reason: str = "invalidated") -> None:
    """使仍有效的认领失效（修订原文/译文、撤展/复展时调用）。

    released_at IS NULL 的行即“尚未结束”的认领（含已到期但未被接手的行），
    统一写入结束时间与原因；同一译文同一时刻至多一行满足条件。
    """
    sql = (
        "UPDATE review_claims SET released_at = ?, end_reason = ? "
        "WHERE released_at IS NULL"
    )
    params: list = [now(), reason]
    if label_id is not None:
        sql += " AND label_id = ?"
        params.append(label_id)
    if language_id is not None:
        sql += " AND language_id = ?"
        params.append(language_id)
    conn.execute(sql, params)


def _active_claims_map(conn, language_id: int, viewer_id: Optional[int],
                       ts: Optional[str] = None) -> dict:
    """取某语言下所有“有效”认领（未结束且未到期），键为 label_id。"""
    ts = ts or now()
    rows = conn.execute(
        "SELECT c.*, u.display_name AS claimer_name, u.username AS claimer_username "
        "FROM review_claims c JOIN users u ON u.id = c.claimer_id "
        "WHERE c.language_id = ? AND c.released_at IS NULL AND c.expires_at > ?",
        (language_id, ts),
    ).fetchall()
    result = {}
    for r in rows:
        d = dict(r)
        d["mine"] = viewer_id is not None and r["claimer_id"] == viewer_id
        result[r["label_id"]] = d
    return result


# ---------------------------- 多语同批发布：状态指纹与校验凭证 ----------------------------
#
# 预览时对“当前全部在展展签 × 所选各语言”的发布相关状态计算指纹并打包成凭证
# （HMAC-SHA256 签名，密钥存于 app_meta，每库一个）；提交时凭同一把写锁重放计算，
# 任一位变化（原文/译文/确认/展签在展状态/所选语言最新发布版本）即整批拒绝。


def _canonical(obj) -> bytes:
    """确定性 JSON 序列化：键排序、紧凑分隔，保证同值同字节。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _batch_secret(conn) -> str:
    """取本库的凭证签名密钥；缺失时补一个（兼容手工建库）。"""
    row = conn.execute(
        "SELECT value FROM app_meta WHERE key = 'batch_token_secret'"
    ).fetchone()
    if row:
        return row["value"]
    secret = secrets.token_hex(32)
    conn.execute(
        "INSERT OR IGNORE INTO app_meta (key, value) VALUES ('batch_token_secret', ?)",
        (secret,),
    )
    row = conn.execute(
        "SELECT value FROM app_meta WHERE key = 'batch_token_secret'"
    ).fetchone()
    return row["value"] if row else secret


def _on_display_labels(conn) -> list:
    """当前全部在展展签，按编号升序（预览与提交共用，保证口径一致）。"""
    return conn.execute(
        "SELECT * FROM labels WHERE status = 'on_display' ORDER BY id"
    ).fetchall()


def _source_hash(label) -> str:
    return hashlib.sha256(_canonical({
        "t": label["source_text"],
        "rev": label["source_rev"],
    })).hexdigest()


def _item_status(label, t) -> tuple[str, str]:
    """单展签×语言的就绪状态与原因（与 build_board 口径一致，另加“非提交人确认”硬校验）。"""
    if t is None:
        return "missing", "尚无译文"
    if t["source_rev_at_edit"] != label["source_rev"]:
        return "stale", f"译文基于原文修订 {t['source_rev_at_edit']}，当前为 {label['source_rev']}"
    if not t["confirmed"]:
        return "pending", "译文待校对确认"
    if t["confirmer_id"] is None or t["confirmer_id"] == t["translator_id"]:
        # 正常流程已禁止自审，这里作为发布前的最终防线
        return "pending", "译文须由非提交人确认"
    return "ready", "已确认，可发布"


def _item_hash(label, t) -> str:
    return hashlib.sha256(_canonical({
        "s": label["source_text"],
        "srev": label["source_rev"],
        "t": None if t is None else t["text"],
        "trev": None if t is None else t["trans_rev"],
        "srev_at": None if t is None else t["source_rev_at_edit"],
        "ok": None if t is None else bool(t["confirmed"]),
        "cid": None if t is None else t["confirmer_id"],
        "tid": None if t is None else t["translator_id"],
        "hid": None if t is None else bool(t["public_hidden"]),
    })).hexdigest()


def _lang_fingerprint(conn, lang_id: int, labels: list) -> dict:
    """某语言相对给定在展展签集合的指纹、逐条状态与最新发布版本。"""
    trans_rows = conn.execute(
        "SELECT * FROM translations WHERE language_id = ?", (lang_id,)
    ).fetchall()
    tmap = {t["label_id"]: t for t in trans_rows}
    items: dict[str, dict] = {}
    for lb in labels:
        t = tmap.get(lb["id"])
        status, reason = _item_status(lb, t)
        items[str(lb["id"])] = {
            "st": status,
            "rsn": reason,
            "h": _item_hash(lb, t),
        }
    latest = conn.execute(
        "SELECT MAX(version) AS v FROM snapshots WHERE language_id = ?", (lang_id,)
    ).fetchone()["v"]
    return {"items": items, "latest": latest}


def _batch_state(conn, lang_ids: list[int]) -> dict:
    labels = _on_display_labels(conn)
    return {
        "labels": {str(lb["id"]): {"h": _source_hash(lb)} for lb in labels},
        "langs": {str(lid): _lang_fingerprint(conn, lid, labels) for lid in lang_ids},
    }


def _issue_credential(conn, *, lang_ids: list[int], state: dict, user_id: int) -> str:
    payload = {
        "v": 1,
        "u": user_id,
        "ids": lang_ids,
        "st": state,
        "iat": now(),
    }
    body = _b64url(_canonical(payload))
    sig = hmac.new(_batch_secret(conn).encode(), body.encode(), hashlib.sha256).digest()
    return body + "." + _b64url(sig)


def _read_credential(conn, credential: str) -> Optional[dict]:
    """校验并解析凭证；签名错误/格式损坏/字段缺失一律返回 None。"""
    try:
        body_b64, sig_b64 = credential.split(".", 1)
        expected = hmac.new(_batch_secret(conn).encode(), body_b64.encode(),
                            hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64url_decode(sig_b64)):
            return None
        data = json.loads(_b64url_decode(body_b64))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("st"), dict) \
            or not isinstance(data.get("ids"), list) or not isinstance(data.get("u"), int):
        return None
    return data


_STATUS_CN = {"missing": "缺译", "stale": "译文过期", "pending": "待审", "ready": "已确认"}
_ITEM_HINT = {
    "missing": "缺少译文",
    "stale": "译文已不对应最新原文（过期）",
    "pending": "尚未由非提交人确认（待审）",
    "ready": "该条状态已变化，请重新预览",
}


# ---------------------------- 应用生命周期 ----------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="展馆多语展签审校系统", lifespan=lifespan)


# ---------------------------- 认证辅助 ----------------------------

SESSION_COOKIE = "exhibit_session"


def current_user(request: Request) -> dict:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="未登录")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ?",
            (token,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="会话已失效，请重新登录")
    return dict(row)


def require_roles(*roles: str):
    def dep(user: dict = Depends(current_user)) -> dict:
        if user["role"] not in roles:
            raise HTTPException(status_code=403, detail="当前角色无权执行该操作")
        return user
    return dep


def public_user(request: Request) -> Optional[dict]:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ?",
            (token,),
        ).fetchone()
    return dict(row) if row else None


# ---------------------------- 登录 / 登出 / 我 ----------------------------


@app.post("/api/login")
def login(body: LoginBody):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (body.username.strip(),)).fetchone()
        if not row or not secrets.compare_digest(
            row["pw_hash"], hash_password(body.password, row["salt"])
        ):
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        token = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO sessions (token, user_id, created_at) VALUES (?,?,?)",
            (token, row["id"], now()),
        )
        conn.commit()
    resp = JSONResponse({
        "id": row["id"], "username": row["username"],
        "display_name": row["display_name"], "role": row["role"],
    })
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax")
    return resp


@app.post("/api/logout")
def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        with get_conn() as conn:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            conn.commit()
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.get("/api/me")
def me(user: dict = Depends(current_user)):
    return {
        "id": user["id"], "username": user["username"],
        "display_name": user["display_name"], "role": user["role"],
    }


# ---------------------------- 展签（原文） ----------------------------


@app.get("/api/labels")
def list_labels(user: dict = Depends(current_user)):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT l.*, uw.display_name AS withdrawn_by_name, "
            "       ur.display_name AS restored_by_name "
            "FROM labels l "
            "LEFT JOIN users uw ON uw.id = l.withdrawn_by "
            "LEFT JOIN users ur ON ur.id = l.restored_by "
            "ORDER BY l.id"
        ).fetchall()
    return [dict(r) for r in rows]


def _get_label_with_names(conn, label_id: int):
    return conn.execute(
        "SELECT l.*, uw.display_name AS withdrawn_by_name, "
        "       ur.display_name AS restored_by_name "
        "FROM labels l "
        "LEFT JOIN users uw ON uw.id = l.withdrawn_by "
        "LEFT JOIN users ur ON ur.id = l.restored_by "
        "WHERE l.id = ?",
        (label_id,),
    ).fetchone()


@app.post("/api/labels")
def create_label(body: LabelBody, user: dict = Depends(require_roles("librarian"))):
    with db._write_lock, get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO labels (source_text, source_rev, created_at, updated_at) VALUES (?,1,?,?)",
            (body.source_text.strip(), now(), now()),
        )
        conn.commit()
        label_id = cur.lastrowid
    return {"id": label_id, "source_rev": 1}


@app.put("/api/labels/{label_id}")
def update_label(label_id: int, body: LabelBody, user: dict = Depends(require_roles("librarian"))):
    """修改原文：原文修订号 +1，该展签全部语言的译文确认失效（confirmed=0）。

    即使新文本与旧文本完全相同也照样递增修订号，使所有旧确认失效。
    请求必须携带读取时的 source_rev，否则视为过期请求拒绝（保留现状）。
    """
    with db._write_lock, get_conn() as conn:
        row = conn.execute("SELECT * FROM labels WHERE id = ?", (label_id,)).fetchone()
        if not row:
            raise HTTPException(404, "展签不存在")
        if body.source_rev != row["source_rev"]:
            raise HTTPException(
                409,
                f"原文已被他人修改（服务器修订号 {row['source_rev']}，"
                f"请求基于 {body.source_rev}），请刷新后重试",
            )
        conn.execute(
            "UPDATE labels SET source_text = ?, source_rev = source_rev + 1, updated_at = ? WHERE id = ?",
            (body.source_text.strip(), now(), label_id),
        )
        # 旧译文确认全部失效（译文文本保留，状态变为“过期”）
        conn.execute(
            "UPDATE translations SET confirmed = 0, confirmer_id = NULL "
            "WHERE label_id = ? AND confirmed = 1",
            (label_id,),
        )
        # 原文修订：该展签各语言的待审认领全部失效
        _invalidate_claims(conn, label_id=label_id)
        conn.commit()
        new_row = conn.execute("SELECT * FROM labels WHERE id = ?", (label_id,)).fetchone()
    return dict(new_row)


# ---------------------------- 撤展 / 复展 ----------------------------


@app.post("/api/labels/{label_id}/withdraw")
def withdraw_label(
    label_id: int, body: WithdrawBody,
    user: dict = Depends(require_roles("librarian")),
):
    """馆员撤展：按展签编号 + 读取时的状态版本 + 原因撤展。

    - 状态版本过期 / 已是撤展状态（重复变更）→ 409，数据不变；
    - 无权 → 403；编号无效 → 404；原因为空 → 422。
    - 立即对所有语言生效：游客看到的“最新发布内容”中该展签被隐藏
      （历史快照、已登录用户查询快照的内容均不改变）。
    - 译文文本与确认保留（看板不再展示待办，复展时另行清除确认）。
    """
    reason = body.reason.strip()
    if not reason:
        raise HTTPException(422, "撤展原因不能为空")
    with db._write_lock, get_conn() as conn:
        row = conn.execute("SELECT * FROM labels WHERE id = ?", (label_id,)).fetchone()
        if not row:
            raise HTTPException(404, "展签编号无效")
        if body.status_rev != row["status_rev"]:
            raise HTTPException(
                409,
                f"展签状态已被他人变更（服务器状态版本 {row['status_rev']}，"
                f"请求基于 {body.status_rev}），请刷新后重试",
            )
        if row["status"] == "withdrawn":
            raise HTTPException(409, "该展签已撤展，请勿重复撤展")

        ts = now()
        conn.execute(
            "UPDATE labels SET status = 'withdrawn', status_rev = status_rev + 1, "
            "withdrawn_reason = ?, withdrawn_by = ?, withdrawn_at = ?, "
            "restored_by = NULL, restored_at = NULL WHERE id = ?",
            (reason, user["id"], ts, label_id),
        )
        # 对所有语言立即从游客最新发布内容中隐藏（历史快照 JSON 不动）
        conn.execute(
            "UPDATE translations SET public_hidden = 1 WHERE label_id = ?",
            (label_id,),
        )
        # 撤展：该展签各语言认领全部失效
        _invalidate_claims(conn, label_id=label_id)
        conn.commit()
        new_row = _get_label_with_names(conn, label_id)
    return dict(new_row)


@app.post("/api/labels/{label_id}/restore")
def restore_label(
    label_id: int, body: RestoreBody,
    user: dict = Depends(require_roles("librarian")),
):
    """馆员复展：按展签编号 + 状态版本复展。

    - 状态版本过期 / 本就在展（重复变更）→ 409；无权 → 403；编号无效 → 404。
    - 保留各语言译文文本，但清除该展签各语言的确认；
      游客在“译文按当前原文经他人重新确认并发布新快照”之前仍看不到它
      （public_hidden 维持为 1，随各语言下一次含该条的快照发布清除）。
    """
    with db._write_lock, get_conn() as conn:
        row = conn.execute("SELECT * FROM labels WHERE id = ?", (label_id,)).fetchone()
        if not row:
            raise HTTPException(404, "展签编号无效")
        if body.status_rev != row["status_rev"]:
            raise HTTPException(
                409,
                f"展签状态已被他人变更（服务器状态版本 {row['status_rev']}，"
                f"请求基于 {body.status_rev}），请刷新后重试",
            )
        if row["status"] == "on_display":
            raise HTTPException(409, "该展签当前在展，无需复展")

        ts = now()
        conn.execute(
            "UPDATE labels SET status = 'on_display', status_rev = status_rev + 1, "
            "restored_by = ?, restored_at = ? WHERE id = ?",
            (user["id"], ts, label_id),
        )
        # 保留译文，清除该展签各语言的确认（须重新经他人确认）
        conn.execute(
            "UPDATE translations SET confirmed = 0, confirmer_id = NULL WHERE label_id = ?",
            (label_id,),
        )
        # 复展：该展签各语言认领全部失效（确认被清，回到待审须重新认领）
        _invalidate_claims(conn, label_id=label_id)
        conn.commit()
        new_row = _get_label_with_names(conn, label_id)
    return dict(new_row)


# ---------------------------- 语言 ----------------------------


@app.get("/api/languages")
def list_languages(user: dict = Depends(current_user)):
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM languages ORDER BY id").fetchall()
    return [dict(r) for r in rows]


@app.post("/api/languages")
def create_language(body: LanguageBody, user: dict = Depends(require_roles("librarian"))):
    code = body.code.strip().lower()
    with db._write_lock, get_conn() as conn:
        exists = conn.execute("SELECT 1 FROM languages WHERE code = ?", (code,)).fetchone()
        if exists:
            raise HTTPException(409, f"语言代码 {code} 已存在")
        cur = conn.execute(
            "INSERT INTO languages (code, name, created_at) VALUES (?,?,?)",
            (code, body.name.strip(), now()),
        )
        conn.commit()
        lid = cur.lastrowid
    return {"id": lid, "code": code, "name": body.name.strip()}


# ---------------------------- 看板核心：按语言聚合 ----------------------------


def build_board(conn, language_id: int, viewer_id: Optional[int] = None) -> dict:
    lang = conn.execute("SELECT * FROM languages WHERE id = ?", (language_id,)).fetchone()
    if not lang:
        raise HTTPException(404, "语言不存在")

    labels = conn.execute(
        "SELECT l.*, uw.display_name AS withdrawn_by_name, "
        "       ur.display_name AS restored_by_name "
        "FROM labels l "
        "LEFT JOIN users uw ON uw.id = l.withdrawn_by "
        "LEFT JOIN users ur ON ur.id = l.restored_by "
        "ORDER BY l.id"
    ).fetchall()
    trans_rows = conn.execute(
        "SELECT t.*, uu.display_name AS translator_name, "
        "       ur.display_name AS confirmer_name "
        "FROM translations t "
        "LEFT JOIN users uu ON uu.id = t.translator_id "
        "LEFT JOIN users ur ON ur.id = t.confirmer_id "
        "WHERE t.language_id = ?",
        (language_id,),
    ).fetchall()
    trans_map = {t["label_id"]: dict(t) for t in trans_rows}
    # 当前有效认领（未释放、未到期），键为 label_id
    claim_map = _active_claims_map(conn, language_id, viewer_id)

    items = []
    withdrawn_items = []
    counts = {"missing": 0, "pending": 0, "stale": 0, "ready": 0}
    for label in labels:
        if label["status"] == "withdrawn":
            # 撤展展签：不计入待办统计与发布条件，也不写入后续快照
            withdrawn_items.append({
                "label": dict(label),
                "translation": trans_map.get(label["id"]),
                "status": "withdrawn",
                "reason": label["withdrawn_reason"] or "（未填写撤展原因）",
            })
            continue
        t = trans_map.get(label["id"])
        if t is None:
            status = "missing"           # 待译：尚无译文
            reason = "尚无译文"
        elif t["source_rev_at_edit"] != label["source_rev"]:
            status = "stale"             # 过期：译文基于旧版原文
            reason = f"译文基于原文修订 {t['source_rev_at_edit']}，当前为 {label['source_rev']}"
        elif not t["confirmed"]:
            status = "pending"           # 待审：当前原文 + 未确认
            reason = "译文待校对确认"
        else:
            status = "ready"
            reason = "已确认，可发布"
        counts[status] += 1
        items.append({
            "label": dict(label),
            "translation": t,
            "status": status,
            "reason": reason,
            # 仅“在展 + 待审”译文可能带有效认领；过期/已确认/待译/撤展均为空
            "claim": claim_map.get(label["id"]) if status == "pending" else None,
        })

    total = len(items)
    blocking = []
    if not labels:
        blocking.append("该语言下尚无任何展签")
    elif total == 0:
        blocking.append("当前没有在展展签（全部展签均已撤展），不能发布")
    if counts["missing"]:
        blocking.append(f"{counts['missing']} 条待译")
    if counts["stale"]:
        blocking.append(f"{counts['stale']} 条译文基于旧版原文（过期）")
    if counts["pending"]:
        blocking.append(f"{counts['pending']} 条待校对确认")
    publishable = total > 0 and counts["ready"] == total

    # 最新快照
    snap = conn.execute(
        "SELECT s.id, s.version, s.published_at, u.display_name AS published_by_name "
        "FROM snapshots s LEFT JOIN users u ON u.id = s.published_by "
        "WHERE s.language_id = ? ORDER BY s.version DESC LIMIT 1",
        (language_id,),
    ).fetchone()

    return {
        "language": dict(lang),
        "total": total,
        "withdrawn_total": len(withdrawn_items),
        "counts": counts,
        "items": items,
        "withdrawn_items": withdrawn_items,
        "publishable": publishable,
        "blocking_reasons": blocking,
        "latest_snapshot": dict(snap) if snap else None,
    }


@app.get("/api/board/{language_id}")
def board(language_id: int, user: dict = Depends(current_user)):
    with get_conn() as conn:
        return build_board(conn, language_id, user["id"])


@app.get("/api/overview")
def overview(user: dict = Depends(current_user)):
    """各语言汇总 + 全局待办，界面顶部使用。"""
    with get_conn() as conn:
        langs = conn.execute("SELECT * FROM languages ORDER BY id").fetchall()
        result = []
        for lang in langs:
            b = build_board(conn, lang["id"])
            result.append({
                "language": b["language"],
                "total": b["total"],
                "withdrawn_total": b["withdrawn_total"],
                "counts": b["counts"],
                "publishable": b["publishable"],
                "blocking_reasons": b["blocking_reasons"],
                "latest_snapshot": b["latest_snapshot"],
            })
    return result


# ---------------------------- 提交译文 ----------------------------


@app.post("/api/board/{language_id}/labels/{label_id}/translation")
def submit_translation_for_label(
    language_id: int, label_id: int, body: TranslationBody,
    user: dict = Depends(require_roles("translator", "reviewer")),
):
    with db._write_lock, get_conn() as conn:
        label = conn.execute("SELECT * FROM labels WHERE id = ?", (label_id,)).fetchone()
        if not label:
            raise HTTPException(404, "展签不存在")
        if label["status"] == "withdrawn":
            raise HTTPException(409, "该展签已撤展，不能提交或修订译文")
        lang = conn.execute("SELECT 1 FROM languages WHERE id = ?", (language_id,)).fetchone()
        if not lang:
            raise HTTPException(404, "语言不存在")

        # 乐观锁：读取后原文若已被修改，拒绝
        if body.source_rev != label["source_rev"]:
            raise HTTPException(
                409,
                f"原文已更新（当前修订号 {label['source_rev']}，你的请求基于 {body.source_rev}），"
                "请基于最新原文重新翻译",
            )

        existing = conn.execute(
            "SELECT * FROM translations WHERE label_id = ? AND language_id = ?",
            (label_id, language_id),
        ).fetchone()

        if existing:
            # 乐观锁：译文也已被他人更新，拒绝
            if body.trans_rev is None or body.trans_rev != existing["trans_rev"]:
                raise HTTPException(
                    409,
                    f"译文已被更新（当前译文修订号 {existing['trans_rev']}），请刷新后重试",
                )
            conn.execute(
                "UPDATE translations SET text = ?, trans_rev = trans_rev + 1, "
                "source_rev_at_edit = ?, confirmed = 0, confirmer_id = NULL, "
                "translator_id = ?, updated_at = ? WHERE id = ?",
                (body.text.strip(), label["source_rev"], user["id"], now(), existing["id"]),
            )
        else:
            if body.trans_rev not in (None, 1):
                raise HTTPException(409, "译文尚不存在，不应携带译文修订号")
            cur = conn.execute(
                "INSERT INTO translations (label_id, language_id, text, trans_rev, "
                "source_rev_at_edit, confirmed, translator_id, updated_at) "
                "VALUES (?,?,?,1,?,0,?,?)",
                (label_id, language_id, body.text.strip(), label["source_rev"],
                 user["id"], now()),
            )
        # 译文修订号变化：该译文上的认领立即失效
        _invalidate_claims(conn, label_id=label_id, language_id=language_id)
        conn.commit()
        t = conn.execute(
            "SELECT * FROM translations WHERE label_id = ? AND language_id = ?",
            (label_id, language_id),
        ).fetchone()
    return dict(t)


# ---------------------------- 确认 / 取消确认 ----------------------------


@app.post("/api/board/{language_id}/labels/{label_id}/confirm")
def confirm_translation(
    language_id: int, label_id: int, body: ConfirmBody,
    user: dict = Depends(require_roles("translator", "reviewer")),
):
    """校对确认。同一账号不得确认自己提交的译文；须携带读取时的双修订号。"""
    with db._write_lock, get_conn() as conn:
        label = conn.execute("SELECT * FROM labels WHERE id = ?", (label_id,)).fetchone()
        if not label:
            raise HTTPException(404, "展签不存在")
        if label["status"] == "withdrawn":
            raise HTTPException(409, "该展签已撤展，不能确认译文")
        t = conn.execute(
            "SELECT * FROM translations WHERE label_id = ? AND language_id = ?",
            (label_id, language_id),
        ).fetchone()
        if not t:
            raise HTTPException(404, "尚无译文，无法确认")
        if body.source_rev != label["source_rev"]:
            raise HTTPException(409, "原文已有新版本，该译文已过期，请退回译者更新")
        if body.trans_rev != t["trans_rev"]:
            raise HTTPException(409, "译文已被更新，你读取的是旧版本，请刷新后重试")
        if t["source_rev_at_edit"] != label["source_rev"]:
            raise HTTPException(409, "译文基于旧版原文，须先更新译文")
        if t["translator_id"] == user["id"]:
            raise HTTPException(403, "同一账号不得确认自己提交的译文")

        # 有效认领期间，仅认领人可以确认；其他账号一律拒绝
        active = conn.execute(
            "SELECT c.*, u.display_name AS claimer_name FROM review_claims c "
            "JOIN users u ON u.id = c.claimer_id "
            "WHERE c.label_id = ? AND c.language_id = ? "
            "AND c.released_at IS NULL AND c.expires_at > ?",
            (label_id, language_id, now()),
        ).fetchone()
        if active and active["claimer_id"] != user["id"]:
            raise HTTPException(
                409,
                f"该译文已被 {active['claimer_name']} 认领，到期时间 "
                f"{active['expires_at']}（UTC）；认领期间请由认领人校对确认",
            )

        ts = now()
        conn.execute(
            "UPDATE translations SET confirmed = 1, confirmer_id = ?, updated_at = ? WHERE id = ?",
            (user["id"], ts, t["id"]),
        )
        # 成功确认结束认领（仅当前有效认领；已过期行留给接手时覆盖，不改其结束原因）
        conn.execute(
            "UPDATE review_claims SET released_at = ?, end_reason = 'confirmed' "
            "WHERE label_id = ? AND language_id = ? AND released_at IS NULL AND expires_at > ?",
            (ts, label_id, language_id, ts),
        )
        conn.commit()
        t = conn.execute(
            "SELECT t.*, ur.display_name AS confirmer_name FROM translations t "
            "LEFT JOIN users ur ON ur.id = t.confirmer_id WHERE t.id = ?",
            (t["id"],),
        ).fetchone()
    return dict(t)


# ---------------------------- 待审认领 / 释放 ----------------------------


def _claim_row(conn, claim_id: int, viewer_id: Optional[int] = None):
    r = conn.execute(
        "SELECT c.*, u.display_name AS claimer_name, u.username AS claimer_username "
        "FROM review_claims c JOIN users u ON u.id = c.claimer_id WHERE c.id = ?",
        (claim_id,),
    ).fetchone()
    if not r:
        return None
    d = dict(r)
    d["active"] = r["released_at"] is None and r["expires_at"] > now()
    d["mine"] = viewer_id is not None and r["claimer_id"] == viewer_id
    return d


@app.post("/api/board/{language_id}/labels/{label_id}/claim")
def claim_translation(
    language_id: int, label_id: int, body: ClaimBody,
    user: dict = Depends(require_roles("translator", "reviewer")),
):
    """认领待审译文 30 分钟。

    凭展签、语言及读取时的原文/译文双修订号认领：
    - 译文提交者本人不得认领（403，自审禁止的延伸）；
    - 展签撤展（409）、编号/语言无效（404）、双修订号过期（409）、
      译文不存在（404）、译文已过期或已确认（409）一律拒绝且不改变状态；
    - 同一译文已有他人有效认领 → 409（响应中带认领人与到期时间）；
    - 本人已持有有效认领 → 幂等返回原认领（不续期、不改时间）；
    - 前认领已到期或已结束 → 接手：唯一行被新认领覆盖，到期时间顺延 30 分钟。
    """
    with db._write_lock, get_conn() as conn:
        label = conn.execute("SELECT * FROM labels WHERE id = ?", (label_id,)).fetchone()
        if not label:
            raise HTTPException(404, "展签不存在")
        if label["status"] == "withdrawn":
            raise HTTPException(409, "该展签已撤展，译文不可认领")
        if not conn.execute("SELECT 1 FROM languages WHERE id = ?", (language_id,)).fetchone():
            raise HTTPException(404, "语言不存在")
        t = conn.execute(
            "SELECT * FROM translations WHERE label_id = ? AND language_id = ?",
            (label_id, language_id),
        ).fetchone()
        if not t:
            raise HTTPException(404, "尚无译文，无法认领")
        if body.source_rev != label["source_rev"]:
            raise HTTPException(409, "原文已有新版本，请刷新后按当前修订号重新认领")
        if body.trans_rev != t["trans_rev"]:
            raise HTTPException(409, "译文已被修订，请刷新后按当前译文修订号重新认领")
        if t["source_rev_at_edit"] != label["source_rev"]:
            raise HTTPException(409, "该译文基于旧版原文（过期），须先更新译文，不能认领")
        if t["confirmed"]:
            raise HTTPException(409, "该译文已确认，无需认领")
        if t["translator_id"] == user["id"]:
            raise HTTPException(403, "提交该译文的账号不得认领自己的译文")

        ts = now()
        existing = conn.execute(
            "SELECT * FROM review_claims WHERE label_id = ? AND language_id = ?",
            (label_id, language_id),
        ).fetchone()
        if existing and existing["released_at"] is None and existing["expires_at"] > ts:
            # 有效认领存在
            if existing["claimer_id"] == user["id"]:
                return _claim_row(conn, existing["id"], user["id"])
            holder = conn.execute(
                "SELECT display_name FROM users WHERE id = ?",
                (existing["claimer_id"],),
            ).fetchone()
            raise HTTPException(
                409,
                f"该译文已被 {holder['display_name'] if holder else '#' + str(existing['claimer_id'])} "
                f"认领，到期时间 {existing['expires_at']}（UTC）；到期后可接手",
            )

        claimed_at = ts
        expires = (_parse_ts(ts) + timedelta(seconds=CLAIM_TTL_SECONDS)).strftime(TS_FORMAT)
        if existing:
            # 接手：覆盖同一行（前认领已到期或已结束）
            conn.execute(
                "UPDATE review_claims SET source_rev = ?, trans_rev = ?, claimer_id = ?, "
                "claimed_at = ?, expires_at = ?, released_at = NULL, end_reason = NULL WHERE id = ?",
                (label["source_rev"], t["trans_rev"], user["id"],
                 claimed_at, expires, existing["id"]),
            )
            cid = existing["id"]
        else:
            cur = conn.execute(
                "INSERT INTO review_claims (label_id, language_id, source_rev, trans_rev, "
                "claimer_id, claimed_at, expires_at) VALUES (?,?,?,?,?,?,?)",
                (label_id, language_id, label["source_rev"], t["trans_rev"],
                 user["id"], claimed_at, expires),
            )
            cid = cur.lastrowid
        conn.commit()
        row = _claim_row(conn, cid, user["id"])
    return row


@app.post("/api/board/{language_id}/labels/{label_id}/claim/release")
def release_translation_claim(
    language_id: int, label_id: int,
    user: dict = Depends(require_roles("translator", "reviewer")),
):
    """认领人提前释放认领。仅当前有效认领的持有账号可释放；释放后他人可立即认领。"""
    with db._write_lock, get_conn() as conn:
        existing = conn.execute(
            "SELECT * FROM review_claims WHERE label_id = ? AND language_id = ?",
            (label_id, language_id),
        ).fetchone()
        if not existing:
            raise HTTPException(404, "该译文尚无认领记录")
        if existing["released_at"] is not None or existing["expires_at"] <= now():
            raise HTTPException(409, "该译文当前没有有效认领，无需释放")
        if existing["claimer_id"] != user["id"]:
            raise HTTPException(403, "只有认领人本人可以释放该认领")
        conn.execute(
            "UPDATE review_claims SET released_at = ?, end_reason = 'released' WHERE id = ?",
            (now(), existing["id"]),
        )
        conn.commit()
        row = _claim_row(conn, existing["id"], user["id"])
    return row


# ---------------------------- 发布不可变快照 ----------------------------


@app.get("/api/snapshots")
def list_snapshots(user: dict = Depends(current_user)):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT s.id, s.language_id, s.version, s.published_at, s.batch_id, l.code, l.name, "
            "u.display_name AS published_by_name "
            "FROM snapshots s JOIN languages l ON l.id = s.language_id "
            "LEFT JOIN users u ON u.id = s.published_by "
            "ORDER BY s.id DESC"
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/snapshots/{snapshot_id}")
def get_snapshot(snapshot_id: int, user: dict = Depends(current_user)):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM snapshots WHERE id = ?", (snapshot_id,)).fetchone()
        if not row:
            raise HTTPException(404, "快照不存在")
    return {"id": row["id"], "language_id": row["language_id"], "version": row["version"],
            "published_at": row["published_at"], "payload": json.loads(row["payload"])}


def _create_snapshot(conn, b: dict, language_id: int, user_id: int,
                     batch_id: Optional[int]) -> dict:
    """按看板当前就绪内容创建一个不可变语言快照（递增版本号）。

    调用方须已在写锁内确认该语言可发布（b["publishable"]）。
    batch_id 非空表示该快照属于一次多语同批发布。
    随发布清除在展展签在该语言的游客隐藏标记（撤展展签不在 items 中，保持隐藏）。
    """
    last = conn.execute(
        "SELECT MAX(version) AS v FROM snapshots WHERE language_id = ?",
        (language_id,),
    ).fetchone()["v"]
    version = (last or 0) + 1

    entries = []
    for item in b["items"]:
        t = item["translation"]
        entries.append({
            "label_id": item["label"]["id"],
            "source_text": item["label"]["source_text"],
            "source_rev": item["label"]["source_rev"],
            "text": t["text"],
            "trans_rev": t["trans_rev"],
            "translator_id": t["translator_id"],
            "confirmer_id": t["confirmer_id"],
        })
    payload = {
        "language_id": language_id,
        "language_code": b["language"]["code"],
        "language_name": b["language"]["name"],
        "version": version,
        "entries": entries,
    }
    cur = conn.execute(
        "INSERT INTO snapshots (language_id, version, published_by, published_at, payload, batch_id) "
        "VALUES (?,?,?,?,?,?)",
        (language_id, version, user_id, now(),
         json.dumps(payload, ensure_ascii=False), batch_id),
    )
    on_display_ids = [item["label"]["id"] for item in b["items"]]
    if on_display_ids:
        placeholders = ",".join("?" for _ in on_display_ids)
        conn.execute(
            f"UPDATE translations SET public_hidden = 0 "
            f"WHERE language_id = ? AND label_id IN ({placeholders})",
            [language_id, *on_display_ids],
        )
    return {"snapshot_id": cur.lastrowid, "version": version}


@app.post("/api/board/{language_id}/publish")
def publish_snapshot(
    language_id: int, user: dict = Depends(require_roles("librarian"))
):
    """只有该语言全部展签译文均对应当前原文修订且已确认，才可发布。
    快照一经发布不可修改（应用层不提供任何变更/删除接口）。"""
    with db._write_lock, get_conn() as conn:
        b = build_board(conn, language_id)
        if not b["publishable"]:
            raise HTTPException(409, {
                "message": "当前语言尚不可发布",
                "reasons": b["blocking_reasons"],
            })
        created = _create_snapshot(conn, b, language_id, user["id"], None)
        conn.commit()
    return {"id": created["snapshot_id"], "version": created["version"]}


# ---------------------------- 多语同批发布：预览 / 提交 / 查询 ----------------------------


def _batch_reject(message: str, reasons: list[str]):
    """整批拒绝：409，detail 固定携带 message 与 reasons（不产生任何快照）。"""
    return HTTPException(409, {"message": message, "reasons": reasons})


@app.post("/api/publish-batches/preview")
def preview_batch(body: BatchPublishBody, user: dict = Depends(require_roles("librarian"))):
    """馆员预览同批发布：至少两种不同目标语言。

    - 未知语言 404；语言编号重复 409；不足两种 422（Pydantic）；
    - 当前没有任何在展展签 409（无在展展签应拒绝）。
    - 预览不发布任何内容，只返回各语言就绪情况、阻断原因与下一个版本号，
      以及一次性的校验凭证 credential（凭该凭证调用提交接口）。
    """
    with db._write_lock, get_tx_conn() as conn:
        conn.execute("BEGIN")
        lang_ids = body.language_ids
        if len(lang_ids) < 2:
            raise HTTPException(422, "同批发布至少选择两种不同目标语言")
        if len(set(lang_ids)) != len(lang_ids):
            raise HTTPException(409, "所选语言存在重复编号，每种语言只能选择一次")
        langs = []
        for lid in lang_ids:
            row = conn.execute("SELECT * FROM languages WHERE id = ?", (lid,)).fetchone()
            if not row:
                raise HTTPException(404, f"语言编号 {lid} 不存在")
            langs.append(row)

        labels = _on_display_labels(conn)
        if not labels:
            raise _batch_reject(
                "当前没有在展展签（全部展签均已撤展或尚未建档），不能发布",
                ["当前没有在展展签，不能发布任何语言快照"],
            )

        languages_out = []
        ready_all = True
        for lid, lang in zip(lang_ids, langs):
            b = build_board(conn, lid)
            # 看板口径之外，补一条“确认人非提交人”的发布前硬校验
            extra = []
            for item in b["items"]:
                t = item["translation"]
                if t and t["confirmed"] and t["confirmer_id"] == t["translator_id"]:
                    extra.append(
                        f"展签 #{item['label']['id']} 的确认人即译文提交人，须由他人重新确认"
                    )
            reasons = list(b["blocking_reasons"]) + extra
            ready = b["publishable"] and not extra
            ready_all = ready_all and ready
            next_version = ((b["latest_snapshot"] or {}).get("version") or 0) + 1
            languages_out.append({
                "language_id": lid,
                "code": lang["code"],
                "name": lang["name"],
                "ready": ready,
                "total": b["total"],
                "counts": b["counts"],
                "next_version": next_version,
                "current_version": (b["latest_snapshot"] or {}).get("version"),
                "blocking_reasons": reasons,
            })

        state = _batch_state(conn, lang_ids)
        credential = _issue_credential(
            conn, lang_ids=lang_ids, state=state, user_id=user["id"]
        )
    return {
        "ready": ready_all,
        "on_display_total": len(labels),
        "credential": credential,
        "languages": languages_out,
    }


@app.post("/api/publish-batches")
def commit_batch(body: BatchCommitBody, user: dict = Depends(require_roles("librarian"))):
    """凭预览凭证整批提交：各语言同时通过才一次生成全部快照。

    任一语言缺译/过期/待审，或预览后相关原文、译文、确认、展签在展状态、
    所选语言最新发布版本发生变化，整批 409 并指出原因，不产生任何部分快照。
    """
    with db._write_lock, get_tx_conn() as conn:
        conn.execute("BEGIN")
        data = _read_credential(conn, body.credential)
        if data is None:
            raise _batch_reject(
                "校验凭证无效或已损坏",
                ["校验凭证无效：请重新调用预览接口获取凭证"],
            )
        if data["u"] != user["id"]:
            raise _batch_reject(
                "校验凭证不属于当前账号",
                ["该凭证由其他馆员预览签发，请重新预览"],
            )
        lang_ids = data["ids"]
        old_state = data["st"]

        for lid in lang_ids:
            if not conn.execute("SELECT 1 FROM languages WHERE id = ?", (lid,)).fetchone():
                raise _batch_reject(
                    "所选语言已不存在", [f"语言编号 {lid} 已被删除，无法发布，请重新预览"]
                )

        # 1) 全局变化：在展展签集合与原文（编号、文本、原文修订号）
        labels = _on_display_labels(conn)
        cur_ids = {lb["id"] for lb in labels}
        old_ids = {int(k) for k in old_state["labels"].keys()}
        global_changes: list[str] = []
        for gone in sorted(old_ids - cur_ids):
            global_changes.append(f"展签 #{gone} 预览后已撤展（在展状态变化）")
        for added in sorted(cur_ids - old_ids):
            global_changes.append(f"展签 #{added} 预览后新增在展（在展展签集合变化）")
        for lb in labels:
            old_lb = old_state["labels"].get(str(lb["id"]))
            if old_lb and old_lb["h"] != _source_hash(lb):
                global_changes.append(
                    f"展签 #{lb['id']} 的原文在预览后已修改（原文修订号现为 {lb['source_rev']}）"
                )

        # 2) 逐语言变化：最新发布版本 + 每条译文/确认，并汇总当前阻断原因
        per_lang_changes: list[str] = []
        blockers: list[str] = []
        boards: dict[int, dict] = {}
        for lid in lang_ids:
            lang = conn.execute("SELECT * FROM languages WHERE id = ?", (lid,)).fetchone()
            old_lang = old_state["langs"].get(str(lid))
            cur_lang = _lang_fingerprint(conn, lid, labels)
            label_name = lang["name"] if lang else f"#{lid}"

            if old_lang is None:
                per_lang_changes.append(f"{label_name}：预览凭证中不含该语言，请重新预览")
                continue
            if old_lang["latest"] != cur_lang["latest"]:
                per_lang_changes.append(
                    f"{label_name}：预览后该语言已发布新版本"
                    f"（v{old_lang['latest']} → v{cur_lang['latest']}），请重新预览"
                )
            old_items = old_lang["items"]
            cur_items = cur_lang["items"]
            for iid, cur_item in cur_items.items():
                old_item = old_items.get(iid)
                if old_item is None:
                    per_lang_changes.append(
                        f"{label_name}：展签 #{iid} 预览后新增，不在预览范围内"
                    )
                    continue
                if old_item["h"] != cur_item["h"]:
                    per_lang_changes.append(
                        f"{label_name}：展签 #{iid} 预览后译文或确认状态变化"
                        f"（当前为{_STATUS_CN.get(cur_item['st'], cur_item['st'])}）"
                    )
            for iid in old_items.keys() - cur_items.keys():
                per_lang_changes.append(
                    f"{label_name}：展签 #{iid} 预览后撤出在展范围"
                )

            # 当前就绪情况（缺译/过期/待审整批拒绝并指出原因）
            b = build_board(conn, lid)
            boards[lid] = b
            extra = []
            for item in b["items"]:
                t = item["translation"]
                if t and t["confirmed"] and t["confirmer_id"] == t["translator_id"]:
                    extra.append(
                        f"展签 #{item['label']['id']} 的确认人即译文提交人，须由他人重新确认"
                    )
            for reason in list(b["blocking_reasons"]) + extra:
                blockers.append(f"{label_name}：{reason}")

        if global_changes or per_lang_changes:
            raise _batch_reject(
                "预览后发布相关状态已变化，整批拒绝，未生成任何快照",
                global_changes + per_lang_changes,
            )
        if blockers:
            raise _batch_reject(
                "所选语言未全部就绪，整批拒绝，未生成任何快照", blockers
            )

        # 3) 全部通过：单事务一次性生成各语言独立递增的不可变快照
        ts = now()
        bcur = conn.execute(
            "INSERT INTO publish_batches (language_ids, credential_issued_at, "
            "submitted_by, submitted_at) VALUES (?,?,?,?)",
            (json.dumps(lang_ids), data.get("iat"), user["id"], ts),
        )
        batch_id = bcur.lastrowid
        versions = []
        for lid in lang_ids:
            created = _create_snapshot(conn, boards[lid], lid, user["id"], batch_id)
            lang = conn.execute("SELECT * FROM languages WHERE id = ?", (lid,)).fetchone()
            versions.append({
                "language_id": lid,
                "code": lang["code"],
                "name": lang["name"],
                "snapshot_id": created["snapshot_id"],
                "version": created["version"],
            })
        conn.commit()
    return {
        "batch_no": batch_id,
        "submitted_at": ts,
        "languages": versions,
    }


@app.get("/api/publish-batches")
def list_publish_batches(user: dict = Depends(current_user)):
    """批次历史：按批次号倒序。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT pb.*, u.display_name AS submitted_by_name "
            "FROM publish_batches pb LEFT JOIN users u ON u.id = pb.submitted_by "
            "ORDER BY pb.id DESC"
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            snap_rows = conn.execute(
                "SELECT s.id AS snapshot_id, s.language_id, s.version, l.code, l.name "
                "FROM snapshots s JOIN languages l ON l.id = s.language_id "
                "WHERE s.batch_id = ? ORDER BY s.id",
                (r["id"],),
            ).fetchall()
            d["language_ids"] = json.loads(r["language_ids"])
            d["snapshots"] = [dict(s) for s in snap_rows]
            result.append(d)
    return result


@app.get("/api/publish-batches/{batch_no}")
def get_publish_batch(batch_no: int, user: dict = Depends(current_user)):
    with get_conn() as conn:
        r = conn.execute(
            "SELECT pb.*, u.display_name AS submitted_by_name "
            "FROM publish_batches pb LEFT JOIN users u ON u.id = pb.submitted_by "
            "WHERE pb.id = ?",
            (batch_no,),
        ).fetchone()
        if not r:
            raise HTTPException(404, "批次不存在")
        d = dict(r)
        snap_rows = conn.execute(
            "SELECT s.id AS snapshot_id, s.language_id, s.version, l.code, l.name, "
            "s.published_at, s.payload "
            "FROM snapshots s JOIN languages l ON l.id = s.language_id "
            "WHERE s.batch_id = ? ORDER BY s.id",
            (batch_no,),
        ).fetchall()
        d["language_ids"] = json.loads(r["language_ids"])
        d["snapshots"] = [{
            "snapshot_id": s["snapshot_id"],
            "language_id": s["language_id"],
            "code": s["code"],
            "name": s["name"],
            "version": s["version"],
            "published_at": s["published_at"],
            "payload": json.loads(s["payload"]),
        } for s in snap_rows]
    return d


# ---------------------------- 游客公开页（无需登录） ----------------------------


@app.get("/api/public/languages")
def public_languages():
    """游客页：每个语言展示最新已发布快照；未发布的语言标注暂无内容。

    已撤展（或复展后尚未随新快照重新发布）的展签，立即从最新内容中隐藏；
    隐藏通过 translations.public_hidden 按语言实现，快照 JSON 本身不修改，
    因此历史快照与已登录用户的快照查询字段/内容均保持不变。

    全部读取在同一只读事务（BEGIN）内完成：多语同批发布在单个写事务中提交，
    WAL 下本查询要么读到批次提交前、要么读到提交后的完整状态，
    同一次游客查询绝不会看到同一批次的部分新版本。
    """
    with get_tx_conn() as conn:
        conn.execute("BEGIN")
        try:
            langs = conn.execute("SELECT * FROM languages ORDER BY id").fetchall()
            result = []
            for lang in langs:
                snap = conn.execute(
                    "SELECT * FROM snapshots WHERE language_id = ? ORDER BY version DESC LIMIT 1",
                    (lang["id"],),
                ).fetchone()
                entry = {
                    "language": {"id": lang["id"], "code": lang["code"], "name": lang["name"]},
                    "published": snap is not None,
                }
                if snap:
                    payload = json.loads(snap["payload"])
                    hidden_rows = conn.execute(
                        "SELECT label_id FROM translations "
                        "WHERE language_id = ? AND public_hidden = 1",
                        (lang["id"],),
                    ).fetchall()
                    hidden = {r["label_id"] for r in hidden_rows}
                    entry.update({
                        "snapshot_id": snap["id"],
                        "version": snap["version"],
                        "batch_no": snap["batch_id"],
                        "published_at": snap["published_at"],
                        "entries": [e for e in payload["entries"]
                                    if e["label_id"] not in hidden],
                    })
                result.append(entry)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return result


# ---------------------------- 静态页面 ----------------------------


@app.get("/")
def root(request: Request):
    # 已登录进入工作台，否则进入登录页
    return FileResponse(
        "app/static/app.html" if public_user(request) else "app/static/login.html"
    )


@app.get("/login")
def login_page():
    return FileResponse("app/static/login.html")


@app.get("/app")
def app_page(user: dict = Depends(current_user)):
    return FileResponse("app/static/app.html")


@app.get("/visitor")
def visitor_page():
    return FileResponse("app/static/visitor.html")


app.mount("/static", StaticFiles(directory="app/static"), name="static")
