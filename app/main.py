"""展馆多语展签审校系统 — FastAPI 主应用。

角色：
  librarian  馆员：维护中文原文与语言，发布语言快照
  translator 译者：提交译文、可校对他人译文
  reviewer   校对：确认译文、也可提交译文
任何译者/校对账号均不得确认自己提交的译文。
"""
from __future__ import annotations

import hashlib
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
from .db import get_conn, hash_password, now

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


class BatchPreviewBody(BaseModel):
    language_ids: list[int] = Field(min_length=2)  # 至少两种不同目标语言


class BatchCommitBody(BaseModel):
    token: str = Field(min_length=1)  # 预览返回的校验凭证


# 认领有效期：默认 30 分钟，可由环境变量覆盖（仅便于测试，文档中说明）
CLAIM_TTL_SECONDS = int(os.environ.get("CLAIM_TTL_SECONDS", "1800"))
# 多语言批量发布预览凭证有效期：默认 10 分钟，可由环境变量覆盖（便于测试）
BATCH_PREVIEW_TTL_SECONDS = int(os.environ.get("BATCH_PREVIEW_TTL_SECONDS", "600"))
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


# ---------------------------- 多语言批量发布：校验指纹 ----------------------------


def _labels_state(conn) -> list[dict]:
    """全部展签当前状态指纹：覆盖原文、在展状态及状态版本变化。

    预览后新增/删除展签（条目数或 id 变化）、修改原文（source_rev/text）、
    撤展/复展（status/status_rev）都会使指纹不一致，整批提交被拒绝。
    """
    rows = conn.execute(
        "SELECT id, source_rev, source_text, status, status_rev "
        "FROM labels ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def _language_state(conn, language_id: int) -> dict:
    """某语言的就绪指纹：每条译文的修订/对应原文/确认/确认人/提交者/隐藏标记，
    以及该语言当前最新已发布版本号。预览后任何相关变化都会被识别。"""
    rows = conn.execute(
        "SELECT label_id, trans_rev, source_rev_at_edit, confirmed, public_hidden, "
        "translator_id, confirmer_id FROM translations WHERE language_id = ? ORDER BY label_id",
        (language_id,),
    ).fetchall()
    latest = conn.execute(
        "SELECT MAX(version) AS v FROM snapshots WHERE language_id = ?",
        (language_id,),
    ).fetchone()["v"]
    return {"translations": [dict(r) for r in rows], "latest_version": latest or 0}


def _build_batch_state(conn, language_ids: list[int]) -> dict:
    return {
        "labels": _labels_state(conn),
        "languages": {str(lid): _language_state(conn, lid) for lid in language_ids},
    }


def _diff_labels(old: list[dict], new: list[dict]) -> list[str]:
    """对比预览时与提交时的展签状态，给出可读变化清单。"""
    om = {x["id"]: x for x in old}
    nm = {x["id"]: x for x in new}
    changes = []
    for lid in sorted(set(om) | set(nm)):
        o, n = om.get(lid), nm.get(lid)
        if o is None:
            changes.append(f"展签 #{lid} 为预览后新增")
            continue
        if n is None:
            changes.append(f"展签 #{lid} 在预览后被删除")
            continue
        if o["status"] != n["status"]:
            what = "撤展" if n["status"] == "withdrawn" else "复展"
            changes.append(f"展签 #{lid} 预览后已{what}")
        elif o["status_rev"] != n["status_rev"]:
            changes.append(f"展签 #{lid} 在展状态版本已变化（{o['status_rev']} → {n['status_rev']}）")
        if o["source_rev"] != n["source_rev"]:
            changes.append(f"展签 #{lid} 原文已修订（修订号 {o['source_rev']} → {n['source_rev']}）")
        elif o["source_text"] != n["source_text"]:
            changes.append(f"展签 #{lid} 原文内容已变化")
    return changes


def _diff_language(lang_name: str, old: dict, new: dict) -> list[str]:
    """对比预览时与提交时某语言译文/确认/隐藏标记/最新版本的变化。"""
    changes = []
    om = {x["label_id"]: x for x in old["translations"]}
    nm = {x["label_id"]: x for x in new["translations"]}
    for lid in sorted(set(om) | set(nm)):
        o, n = om.get(lid), nm.get(lid)
        if o is None:
            changes.append(f"{lang_name}：展签 #{lid} 预览后新增了译文")
            continue
        if n is None:
            changes.append(f"{lang_name}：展签 #{lid} 译文在预览后被删除")
            continue
        if o["trans_rev"] != n["trans_rev"]:
            changes.append(f"{lang_name}：展签 #{lid} 译文已修订（译文修订号 {o['trans_rev']} → {n['trans_rev']}）")
        if o["source_rev_at_edit"] != n["source_rev_at_edit"]:
            changes.append(f"{lang_name}：展签 #{lid} 译文所对应原文修订号变化"
                           f"（{o['source_rev_at_edit']} → {n['source_rev_at_edit']}）")
        if o["confirmed"] and not n["confirmed"]:
            changes.append(f"{lang_name}：展签 #{lid} 预览后确认已失效（待重新确认）")
        elif not o["confirmed"] and n["confirmed"]:
            changes.append(f"{lang_name}：展签 #{lid} 预览后新增了确认")
        elif o["confirmer_id"] != n["confirmer_id"]:
            changes.append(f"{lang_name}：展签 #{lid} 确认人发生变化")
        if o["public_hidden"] != n["public_hidden"]:
            changes.append(f"{lang_name}：展签 #{lid} 在展/隐藏状态变化")
        if o["translator_id"] != n["translator_id"]:
            changes.append(f"{lang_name}：展签 #{lid} 译文提交人发生变化")
    if old["latest_version"] != new["latest_version"]:
        changes.append(f"{lang_name}：所选语言最新发布版本已变化"
                       f"（预览时 v{old['latest_version']}，当前 v{new['latest_version']}），请重新预览")
    return changes


def _extra_confirmer_checks(board: dict) -> list[str]:
    """就绪性的纵深校验：已确认译文的确认人必须存在且不是提交人本人。"""
    problems = []
    for item in board["items"]:
        t = item["translation"]
        if item["status"] == "ready" and (
            not t["confirmer_id"]
            or t["confirmer_id"] == t["translator_id"]
        ):
            problems.append(
                f"展签 #{item['label']['id']} 确认无效（须由非提交人确认）"
            )
    return problems


def _insert_snapshot(conn, language_id: int, board: dict, user_id: int,
                     batch_id: Optional[int] = None, ts: Optional[str] = None) -> dict:
    """按看板当前就绪内容生成一条不可变快照（调用方须先校验 publishable）。

    返回 {"id", "version", "payload", "label_ids"}；同时清除本语言在展展签的
    游客隐藏标记（撤展展签不在 items 中，继续隐藏）。批量与单语言发布共用本函数，
    保证快照内容结构一致。
    """
    ts = ts or now()
    last = conn.execute(
        "SELECT MAX(version) AS v FROM snapshots WHERE language_id = ?",
        (language_id,),
    ).fetchone()["v"]
    version = (last or 0) + 1

    entries = []
    for item in board["items"]:
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
        "language_code": board["language"]["code"],
        "language_name": board["language"]["name"],
        "version": version,
        "entries": entries,
    }
    cur = conn.execute(
        "INSERT INTO snapshots (language_id, version, published_by, published_at, payload, batch_id) "
        "VALUES (?,?,?,?,?,?)",
        (language_id, version, user_id, ts, json.dumps(payload, ensure_ascii=False), batch_id),
    )
    on_display_ids = [item["label"]["id"] for item in board["items"]]
    if on_display_ids:
        placeholders = ",".join("?" for _ in on_display_ids)
        conn.execute(
            f"UPDATE translations SET public_hidden = 0 "
            f"WHERE language_id = ? AND label_id IN ({placeholders})",
            [language_id, *on_display_ids],
        )
    return {"id": cur.lastrowid, "version": version, "payload": payload,
            "label_ids": on_display_ids}


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


@app.post("/api/board/{language_id}/publish")
def publish_snapshot(
    language_id: int, user: dict = Depends(require_roles("librarian"))
):
    """只有该语言全部在展展签译文均对应当前原文修订且已确认，才可发布。
    快照一经发布不可修改（应用层不提供任何变更/删除接口）。

    既有单语言发布流程保持不变（batch_id 为空）。
    """
    with db._write_lock, get_conn() as conn:
        b = build_board(conn, language_id)
        if not b["publishable"]:
            raise HTTPException(409, {
                "message": "当前语言尚不可发布",
                "reasons": b["blocking_reasons"],
            })
        created = _insert_snapshot(conn, language_id, b, user["id"])
        conn.commit()
    return {"id": created["id"], "version": created["version"]}


# ---------------------------- 多语言同批发布（预览 → 凭证提交） ----------------------------


@app.post("/api/publish-batches/preview")
def publish_batch_preview(
    body: BatchPreviewBody, user: dict = Depends(require_roles("librarian"))
):
    """馆员选择至少两种不同目标语言，预览同批发布的就绪情况并领取校验凭证。

    - 未知语言 → 404；语言编号重复 → 422；不足两种 → 422（pydantic）；非馆员 → 403。
    - 预览本身不产生任何快照：返回每种语言的就绪状态、阻断原因、统计与将生成的
      下一版本号，以及一次性提交凭证 token（默认 10 分钟有效）。
    - 每种语言必须覆盖“当前全部在展展签”：缺译、过期、待审、无在展展签均在
      对应语言的 ready=false 与 blocking_reasons 中指出，整批 ready=false。
    """
    language_ids = body.language_ids
    if len(set(language_ids)) != len(language_ids):
        raise HTTPException(422, "目标语言不能重复，请选择至少两种不同的语言")

    with db._write_lock, get_conn() as conn:
        langs = []
        for lid in language_ids:
            row = conn.execute("SELECT * FROM languages WHERE id = ?", (lid,)).fetchone()
            if not row:
                raise HTTPException(404, f"语言编号 {lid} 不存在")
            langs.append(dict(row))

        boards = {lid: build_board(conn, lid) for lid in language_ids}
        state = _build_batch_state(conn, language_ids)

        languages_out = []
        for lid, lang in zip(language_ids, langs):
            b = boards[lid]
            extra = _extra_confirmer_checks(b)
            ready = b["publishable"] and not extra
            blocking = list(b["blocking_reasons"]) + extra
            next_version = (
                conn.execute(
                    "SELECT COALESCE(MAX(version),0) AS v FROM snapshots WHERE language_id = ?",
                    (lid,),
                ).fetchone()["v"]
            ) + 1
            languages_out.append({
                "language_id": lid,
                "code": lang["code"],
                "name": lang["name"],
                "ready": ready,
                "blocking_reasons": blocking,
                "counts": b["counts"],
                "total_on_display": b["total"],
                "withdrawn_total": b["withdrawn_total"],
                "current_version": state["languages"][str(lid)]["latest_version"],
                "next_version": next_version,
                "items": [
                    {
                        "label_id": it["label"]["id"],
                        "source_rev": it["label"]["source_rev"],
                        "status": it["status"],
                        "reason": it["reason"],
                        "trans_rev": it["translation"]["trans_rev"] if it["translation"] else None,
                    }
                    for it in b["items"]
                ],
            })

        ts = now()
        token = secrets.token_urlsafe(24)
        expires = (_parse_ts(ts) + timedelta(seconds=BATCH_PREVIEW_TTL_SECONDS)).strftime(TS_FORMAT)
        conn.execute(
            "INSERT INTO publish_previews "
            "(token, language_ids, state_json, created_by, created_at, expires_at) "
            "VALUES (?,?,?,?,?,?)",
            (token, json.dumps(language_ids), json.dumps(state, ensure_ascii=False),
             user["id"], ts, expires),
        )
        conn.commit()

    return {
        "token": token,
        "ready": all(l["ready"] for l in languages_out),
        "language_count": len(language_ids),
        "languages": languages_out,
        "previewed_at": ts,
        "expires_at": expires,
        "hint": "凭 token 调用 POST /api/publish-batches 提交；"
                "预览后原文/译文/确认/展签在展状态或所选语言最新版本若有变化，整批拒绝。",
    }


@app.post("/api/publish-batches")
def publish_batch_commit(
    body: BatchCommitBody, user: dict = Depends(require_roles("librarian"))
):
    """凭预览凭证一次性提交：每种语言各自生成独立递增的不可变快照。

    - 凭证不存在 → 404；已使用/已过期 → 409（请重新预览）。
    - 任一语言缺译、过期、待审、确认人即提交人、无在展展签，或预览后原文、
      译文、确认、展签在展状态、所选语言最新发布版本发生变化 → 整批拒绝（409），
      返回每个语言的当前阻断原因与变化说明，不产生任何部分快照，凭证不被消耗。
    - 全部校验通过：在单个事务/写锁内依次插入各语言新版本快照并登记批次，
      要么全部成功、要么全部失败；成功后凭证标记 consumed，返回批次号与各语言版本。
    """
    with db._write_lock, get_conn() as conn:
        pv = conn.execute("SELECT * FROM publish_previews WHERE token = ?", (body.token,)).fetchone()
        if not pv:
            raise HTTPException(404, "预览凭证不存在，请重新预览")
        if pv["consumed_at"] is not None:
            raise HTTPException(409, {
                "message": "该预览凭证已用于一次成功提交，不能重复使用；请重新预览",
                "consumed_at": pv["consumed_at"],
                "batch_id": pv["batch_id"],
            })
        if pv["expires_at"] <= now():
            raise HTTPException(409, {
                "message": f"预览凭证已过期（到期时间 {pv['expires_at']} UTC），请重新预览",
                "expires_at": pv["expires_at"],
            })

        language_ids = json.loads(pv["language_ids"])
        stored_state = json.loads(pv["state_json"])

        # 1) 校验展签层面的变化（新增/删除、原文、撤展/复展）
        current_labels = _labels_state(conn)
        label_changes = _diff_labels(stored_state["labels"], current_labels)

        # 2) 逐语言校验：指纹变化 + 当前就绪性（缺译/过期/待审/无在展展签/自审）
        lang_changes: dict[int, list[str]] = {}
        boards = {}
        lang_names = {}
        any_block = bool(label_changes)
        for lid in language_ids:
            lang = conn.execute("SELECT * FROM languages WHERE id = ?", (lid,)).fetchone()
            if not lang:
                # 理论上语言只能被新增、无法删除；仍做防御
                label_changes.append(f"语言编号 {lid} 已不存在")
                any_block = True
                continue
            lang_names[lid] = lang["name"]
            cur_state = _language_state(conn, lid)
            changes = _diff_language(lang["name"], stored_state["languages"][str(lid)], cur_state)
            b = build_board(conn, lid)
            boards[lid] = b
            if changes:
                lang_changes[lid] = changes
            reasons = list(b["blocking_reasons"]) + _extra_confirmer_checks(b)
            if changes or not b["publishable"] or reasons:
                any_block = True
            if not b["publishable"] or reasons:
                lang_changes.setdefault(lid, [])
                lang_changes[lid].extend(reasons)

        if any_block:
            raise HTTPException(409, {
                "message": "整批拒绝：预览后状态发生变化或仍有语言未就绪，未产生任何快照",
                "label_changes": label_changes,
                "language_changes": {
                    str(lid): lang_changes.get(lid, []) for lid in language_ids
                },
                "languages": [
                    {
                        "language_id": lid,
                        "name": lang_names.get(lid, f"#{lid}"),
                        "ready": not lang_changes.get(lid) and boards.get(lid, {}).get("publishable", False),
                        "blocking_reasons": lang_changes.get(lid, []),
                        "total_on_display": boards[lid]["total"] if lid in boards else 0,
                        "counts": boards[lid]["counts"] if lid in boards else None,
                    }
                    for lid in language_ids
                ],
            })

        # 3) 全部通过：单事务内生成批次 + 各语言独立递增的不可变快照
        ts = now()
        max_no = conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS m FROM publish_batches"
        ).fetchone()["m"]
        batch_no = f"PB-{max_no + 1:06d}"
        batch_cur = conn.execute(
            "INSERT INTO publish_batches (batch_no, committed_by, published_at, "
            "preview_token, language_count) VALUES (?,?,?,?,?)",
            (batch_no, user["id"], ts, body.token, len(language_ids)),
        )
        batch_id = batch_cur.lastrowid

        results = []
        for lid in language_ids:
            created = _insert_snapshot(
                conn, lid, boards[lid], user["id"], batch_id=batch_id, ts=ts
            )
            conn.execute(
                "INSERT INTO publish_batch_items (batch_id, language_id, snapshot_id, version) "
                "VALUES (?,?,?,?)",
                (batch_id, lid, created["id"], created["version"]),
            )
            results.append({
                "language_id": lid,
                "code": boards[lid]["language"]["code"],
                "name": boards[lid]["language"]["name"],
                "version": created["version"],
                "snapshot_id": created["id"],
                "entry_count": len(created["label_ids"]),
            })

        conn.execute(
            "UPDATE publish_previews SET consumed_at = ?, batch_id = ? WHERE token = ?",
            (ts, batch_id, body.token),
        )
        conn.commit()

    return {
        "batch_no": batch_no,
        "batch_id": batch_id,
        "published_at": ts,
        "published_by": user["display_name"],
        "language_count": len(results),
        "languages": results,
        "immutable": True,
    }


@app.get("/api/publish-batches")
def list_publish_batches(user: dict = Depends(current_user)):
    """批次历史：按批次倒序列出，含各语言本批次快照与版本。"""
    with get_conn() as conn:
        batches = conn.execute(
            "SELECT b.id, b.batch_no, b.published_at, b.language_count, "
            "u.display_name AS committed_by_name "
            "FROM publish_batches b LEFT JOIN users u ON u.id = b.committed_by "
            "ORDER BY b.id DESC"
        ).fetchall()
        result = []
        for b in batches:
            items = conn.execute(
                "SELECT i.language_id, i.snapshot_id, i.version, l.code, l.name "
                "FROM publish_batch_items i JOIN languages l ON l.id = i.language_id "
                "WHERE i.batch_id = ? ORDER BY i.language_id",
                (b["id"],),
            ).fetchall()
            d = dict(b)
            d["items"] = [dict(r) for r in items]
            result.append(d)
    return result


@app.get("/api/publish-batches/{batch_id}")
def get_publish_batch(batch_id: int, user: dict = Depends(current_user)):
    with get_conn() as conn:
        b = conn.execute(
            "SELECT b.*, u.display_name AS committed_by_name "
            "FROM publish_batches b LEFT JOIN users u ON u.id = b.committed_by "
            "WHERE b.id = ?",
            (batch_id,),
        ).fetchone()
        if not b:
            raise HTTPException(404, "发布批次不存在")
        items = conn.execute(
            "SELECT i.language_id, i.snapshot_id, i.version, l.code, l.name "
            "FROM publish_batch_items i JOIN languages l ON l.id = i.language_id "
            "WHERE i.batch_id = ? ORDER BY i.language_id",
            (batch_id,),
        ).fetchall()
    d = dict(b)
    d["items"] = [dict(r) for r in items]
    return d


# ---------------------------- 游客公开页（无需登录） ----------------------------


@app.get("/api/public/languages")
def public_languages():
    """游客页：每个语言展示最新已发布快照；未发布的语言标注暂无内容。

    已撤展（或复展后尚未随新快照重新发布）的展签，立即从最新内容中隐藏；
    隐藏通过 translations.public_hidden 按语言实现，快照 JSON 本身不修改，
    因此历史快照与已登录用户的快照查询字段/内容均保持不变。
    """
    with get_conn() as conn:
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
                    "published_at": snap["published_at"],
                    "entries": [e for e in payload["entries"]
                                if e["label_id"] not in hidden],
                })
            result.append(entry)
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
