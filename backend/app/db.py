"""SQLite 连接与版本化迁移。

设计要点：
- 站点与登录凭证是服务部署环境里的正式存储，连接每次请求独立打开、读已提交，
  开启 WAL 与外键约束，进程重启后数据仍在。
- 结构升级走「版本号 + 迁移记录表」：schema_migrations 记录已落库的版本，
  PRAGMA user_version 作为旧数据版本的外部标记，两者一致后才继续升级。
- 每个版本在一个事务里完成；升级中断时事务整体回滚，下次启动从断点继续，
  不会重复装载样例。
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from app.config import settings

# 当前结构版本。新增结构变更时追加 MIGRATIONS，而不是改老版本的语句。
SCHEMA_VERSION = 2


class ConflictError(Exception):
    """并发修改冲突：版本号对不上或写锁竞争，路由层统一转成 409 回执。"""

    def __init__(self, message: str, *, current_version: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.current_version = current_version


def connect(db_path: str | None = None) -> sqlite3.Connection:
    """打开一个带 WAL、外键与忙等待的 SQLite 连接（自动提交模式，事务显式管理）。"""
    # 每次读环境变量而不是只读一次 settings：测试与多实例部署可以按进程切换库路径。
    path = db_path or os.environ.get("APP_DB_PATH") or settings.db_path
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def db_conn(db_path: str | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def immediate_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """立即拿写锁的事务：站点与凭证关系在同一事务里提交，任一边失败都回滚。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _applied_versions(conn: sqlite3.Connection) -> set[int]:
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {int(row["version"]) for row in rows}


def _exec_ddl(conn: sqlite3.Connection, script: str) -> None:
    """逐条执行 DDL。

    不能用 executescript：它会先隐式提交当前事务，迁移版本就无法和结构变更
    落在同一事务里，中断后做不到整段回滚。SQLite 的 CREATE TABLE/INDEX 本身
    支持事务，逐条 execute 即可。
    """
    for statement in [s.strip() for s in script.split(";") if s.strip()]:
        conn.execute(statement)


def _migration_v1(conn: sqlite3.Connection) -> None:
    _exec_ddl(
        conn,
        """
        CREATE TABLE sites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            site_no TEXT NOT NULL,
            domain TEXT NOT NULL,
            site_type TEXT NOT NULL,
            request_method TEXT,
            charset TEXT,
            header_template TEXT,
            cookie_config TEXT,
            owner TEXT,
            status TEXT NOT NULL,
            pending INTEGER NOT NULL DEFAULT 1,
            abnormal INTEGER NOT NULL DEFAULT 0,
            version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE UNIQUE INDEX ux_sites_site_no ON sites(site_no);

        CREATE TABLE credentials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            credential_no TEXT NOT NULL,
            target_site TEXT,
            auth_method TEXT NOT NULL,
            account_id TEXT,
            valid_days TEXT,
            session_keep TEXT,
            refresh_trigger TEXT,
            expiry_alert TEXT,
            status TEXT NOT NULL,
            pending INTEGER NOT NULL DEFAULT 1,
            abnormal INTEGER NOT NULL DEFAULT 0,
            version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE UNIQUE INDEX ux_credentials_credential_no ON credentials(credential_no);

        CREATE TABLE credential_bindings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            credential_id INTEGER NOT NULL UNIQUE,
            site_id INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (credential_id) REFERENCES credentials(id),
            FOREIGN KEY (site_id) REFERENCES sites(id)
        );

        CREATE TABLE migration_issues (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            module TEXT NOT NULL,
            issue_type TEXT NOT NULL,
            business_no TEXT,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE import_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            site_count INTEGER NOT NULL,
            credential_count INTEGER NOT NULL,
            issue_count INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE UNIQUE INDEX ux_import_batches_hash ON import_batches(source, content_hash);
        """,
    )
    # 首次部署在同一事务里装载样例；中断回滚后重跑仍由批次哈希去重。
    from app.legacy import ingest_seed

    ingest_seed(conn)


def _migration_v2(conn: sqlite3.Connection) -> None:
    # 结构升级示例：给高频筛选列与绑定关系补索引，验证版本区分与断点续跑。
    _exec_ddl(
        conn,
        """
        CREATE INDEX idx_sites_status ON sites(status);
        CREATE INDEX idx_credentials_status ON credentials(status);
        CREATE INDEX idx_credential_bindings_site ON credential_bindings(site_id);
        """,
    )


MIGRATIONS: dict[int, Any] = {
    1: _migration_v1,
    2: _migration_v2,
}

_init_lock = threading.Lock()


def init_db(db_path: str | None = None) -> dict[str, Any]:
    """幂等初始化：建迁移记录、按版本顺序升级、返回本次执行情况。"""
    with _init_lock:
        conn = connect(db_path)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " version INTEGER PRIMARY KEY,"
                " applied_at TEXT NOT NULL DEFAULT (datetime('now'))"
                ")"
            )
            # user_version 是旧库自带的版本标记，用来识别比当前代码更老/更新的数据结构。
            legacy_version = int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)
            applied = _applied_versions(conn)
            if legacy_version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"数据库结构版本 {legacy_version} 高于当前程序支持的 {SCHEMA_VERSION}，"
                    "请先升级程序再启动，避免误改用户数据"
                )
            ran: list[int] = []
            for version in sorted(MIGRATIONS):
                if version in applied:
                    continue
                if version <= legacy_version and legacy_version > 0:
                    # 旧库用 user_version 标记过结构：只登记不重复执行历史版本。
                    conn.execute(
                        "INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)",
                        (version,),
                    )
                    continue
                runner = MIGRATIONS[version]
                conn.execute("BEGIN IMMEDIATE")
                try:
                    runner(conn)
                    conn.execute(
                        "INSERT INTO schema_migrations(version) VALUES (?)", (version,)
                    )
                    conn.execute(f"PRAGMA user_version={version}")
                    conn.execute("COMMIT")
                except Exception:
                    # 个别底层错误会连带事务自动失效，ROLLBACK 失败时无需再抛第二次。
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.OperationalError:
                        pass
                    raise
                ran.append(version)
            return {
                "schema_version": SCHEMA_VERSION,
                "legacy_version": legacy_version,
                "migrations_run": ran,
                "applied": sorted(_applied_versions(conn)),
            }
        finally:
            conn.close()


def is_locked(exc: Exception) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower()
