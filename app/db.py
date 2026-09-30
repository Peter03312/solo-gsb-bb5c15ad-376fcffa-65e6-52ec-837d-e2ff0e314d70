"""SQLite 连接、建表与初始化数据。"""
from __future__ import annotations

import os
import secrets
import sqlite3
import threading
from datetime import datetime, timezone

DB_PATH = os.environ.get("DB_PATH", "/data/exhibit.db")

# SQLite 单写者：用全局锁串行化所有写操作，避免 database is locked。
_write_lock = threading.Lock()


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def get_tx_conn() -> sqlite3.Connection:
    """手动事务连接（autocommit 底层模式）：由调用方显式 BEGIN/COMMIT/ROLLBACK。

    用于需要“整段校验 + 提交”共用同一一致性快照的场景（多语同批发布、
    游客聚合查询），避免 sqlite3 模块在 DML 前隐式 BEGIN 与显式 BEGIN 冲突。
    """
    conn = get_conn()
    conn.isolation_level = None
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('librarian','translator','reviewer')),
    salt TEXT NOT NULL,
    pw_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS languages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,       -- 如 en / ja
    name TEXT NOT NULL,              -- 如 English / 日本語
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS labels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_text TEXT NOT NULL,
    source_rev INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'on_display',  -- on_display 在展 / withdrawn 已撤展
    status_rev INTEGER NOT NULL DEFAULT 1,       -- 状态版本：撤展/复展各 +1（乐观锁）
    withdrawn_reason TEXT,
    withdrawn_by INTEGER REFERENCES users(id),
    withdrawn_at TEXT,
    restored_by INTEGER REFERENCES users(id),
    restored_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS translations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label_id INTEGER NOT NULL REFERENCES labels(id) ON DELETE CASCADE,
    language_id INTEGER NOT NULL REFERENCES languages(id),
    text TEXT NOT NULL,
    trans_rev INTEGER NOT NULL DEFAULT 1,
    source_rev_at_edit INTEGER NOT NULL,  -- 译文最后一次提交时对应的原文修订号
    confirmed INTEGER NOT NULL DEFAULT 0,
    public_hidden INTEGER NOT NULL DEFAULT 0,  -- 游客最新内容是否隐藏（撤展置1，随含该条的新快照发布清0）
    translator_id INTEGER REFERENCES users(id),
    confirmer_id INTEGER REFERENCES users(id),
    updated_at TEXT NOT NULL,
    UNIQUE (label_id, language_id)
);

CREATE TABLE IF NOT EXISTS review_claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label_id INTEGER NOT NULL REFERENCES labels(id) ON DELETE CASCADE,
    language_id INTEGER NOT NULL REFERENCES languages(id) ON DELETE CASCADE,
    source_rev INTEGER NOT NULL,        -- 认领时锁定的原文修订号（读取时所见）
    trans_rev INTEGER NOT NULL,         -- 认领时锁定的译文修订号（读取时所见）
    claimer_id INTEGER NOT NULL REFERENCES users(id),
    claimed_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,           -- 认领到期时间（默认认领后 30 分钟）
    released_at TEXT,                   -- 提前释放/失效时间；NULL 表示尚未结束（可能已过期）
    end_reason TEXT,                    -- released 主动释放 / invalidated 修订·撤复展失效 / confirmed 确认结束
    UNIQUE (label_id, language_id)      -- 同一译文同一时刻仅一行认领记录
);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    language_id INTEGER NOT NULL REFERENCES languages(id),
    version INTEGER NOT NULL,
    published_by INTEGER REFERENCES users(id),
    published_at TEXT NOT NULL,
    payload TEXT NOT NULL,           -- 不可变 JSON
    batch_id INTEGER REFERENCES publish_batches(id),  -- 多语同批发布时所属批次；单语言发布为 NULL
    UNIQUE (language_id, version)
);

-- 多语同批发布：一次提交为每个所选语言各生成一个独立递增的不可变快照
CREATE TABLE IF NOT EXISTS publish_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,                 -- 批次号（全局递增，即返回给馆员的 batch_no）
    language_ids TEXT NOT NULL,                           -- 本批目标语言编号 JSON 数组（按请求顺序）
    credential_issued_at TEXT NOT NULL,                   -- 预览签发校验凭证的时间
    submitted_by INTEGER REFERENCES users(id),
    submitted_at TEXT NOT NULL
);

-- 应用级键值（当前仅存多语同批发布校验凭证的 HMAC 密钥）
CREATE TABLE IF NOT EXISTS app_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def hash_password(password: str, salt: str) -> str:
    import hashlib
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()


def _migrate(conn: sqlite3.Connection) -> None:
    """为老版本数据库补齐本轮新增列（幂等）。"""
    label_cols = {r["name"] for r in conn.execute("PRAGMA table_info(labels)").fetchall()}
    for ddl in (
        "ALTER TABLE labels ADD COLUMN status TEXT NOT NULL DEFAULT 'on_display'",
        "ALTER TABLE labels ADD COLUMN status_rev INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE labels ADD COLUMN withdrawn_reason TEXT",
        "ALTER TABLE labels ADD COLUMN withdrawn_by INTEGER REFERENCES users(id)",
        "ALTER TABLE labels ADD COLUMN withdrawn_at TEXT",
        "ALTER TABLE labels ADD COLUMN restored_by INTEGER REFERENCES users(id)",
        "ALTER TABLE labels ADD COLUMN restored_at TEXT",
    ):
        col = ddl.split("ADD COLUMN")[1].strip().split()[0]
        if col not in label_cols:
            conn.execute(ddl)
    trans_cols = {r["name"] for r in conn.execute("PRAGMA table_info(translations)").fetchall()}
    if "public_hidden" not in trans_cols:
        conn.execute(
            "ALTER TABLE translations ADD COLUMN public_hidden INTEGER NOT NULL DEFAULT 0"
        )
    snap_cols = {r["name"] for r in conn.execute("PRAGMA table_info(snapshots)").fetchall()}
    if "batch_id" not in snap_cols:
        conn.execute(
            "ALTER TABLE snapshots ADD COLUMN batch_id INTEGER REFERENCES publish_batches(id)"
        )

    # 多语同批发布校验凭证的签名密钥（每库一个，重启不变）
    if not conn.execute("SELECT 1 FROM app_meta WHERE key = 'batch_token_secret'").fetchone():
        conn.execute(
            "INSERT INTO app_meta (key, value) VALUES ('batch_token_secret', ?)",
            (secrets.token_hex(32),),
        )


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)

        # 默认账号（可通过环境变量覆盖，README 说明）
        seed_users = [
            ("librarian", "馆员（管理员）", "librarian", os.environ.get("SEED_LIBRARIAN_PW", "lib123")),
            ("translator", "译者小李", "translator", os.environ.get("SEED_TRANSLATOR_PW", "tra123")),
            ("reviewer", "校对老王", "reviewer", os.environ.get("SEED_REVIEWER_PW", "rev123")),
        ]
        for username, display_name, role, pw in seed_users:
            row = conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()
            if not row:
                salt = secrets.token_hex(16)
                conn.execute(
                    "INSERT INTO users (username, display_name, role, salt, pw_hash, created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (username, display_name, role, salt, hash_password(pw, salt), now()),
                )

        # 默认语言
        for code, name in (("en", "English"), ("ja", "日本語")):
            row = conn.execute("SELECT 1 FROM languages WHERE code = ?", (code,)).fetchone()
            if not row:
                conn.execute(
                    "INSERT INTO languages (code, name, created_at) VALUES (?,?,?)",
                    (code, name, now()),
                )

        # 示例展签（仅首次初始化）
        count = conn.execute("SELECT COUNT(*) c FROM labels").fetchone()["c"]
        if count == 0:
            samples = [
                "青花缠枝莲纹瓶：明代永乐年间景德镇官窑制品，釉色浓艳，纹饰繁密。",
                "铜鎏金转经筒：清代藏传佛教法器，筒身錾刻六字真言，内藏经卷。",
                "活字印刷木活字一套：清代木质活字，用于族谱与典籍刷印。",
            ]
            for text in samples:
                conn.execute(
                    "INSERT INTO labels (source_text, source_rev, created_at, updated_at) "
                    "VALUES (?,1,?,?)",
                    (text, now(), now()),
                )
        conn.commit()
