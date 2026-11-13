"""站点与凭证的 SQLite 访问层。

只负责 SQL 与行字典转换；业务规则在 services 里。绑定关系的写入统一走
bind_credential，它在一个立即事务里同时改 credentials / credential_bindings，
任一边失败整体回滚，并用 version 做乐观并发控制。
"""
from __future__ import annotations

import sqlite3
from typing import Any

from app.db import ConflictError


def _site_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "站点编号": row["site_no"],
        "域名地址": row["domain"],
        "站点类型": row["site_type"],
        "请求方式": row["request_method"],
        "字符编码": row["charset"],
        "请求头模板": row["header_template"],
        "Cookie配置": row["cookie_config"],
        "站点负责人": row["owner"],
        "status": row["status"],
        "pending": bool(row["pending"]),
        "abnormal": bool(row["abnormal"]),
        "version": row["version"],
    }


def _credential_row(row: sqlite3.Row) -> dict[str, Any]:
    data = {
        "id": row["id"],
        "凭证编号": row["credential_no"],
        "目标站点": row["target_site"],
        "认证方式": row["auth_method"],
        "账号标识": row["account_id"],
        "凭据有效天数": row["valid_days"],
        "会话保持": row["session_keep"],
        "刷新触发": row["refresh_trigger"],
        "失效告警": row["expiry_alert"],
        "status": row["status"],
        "pending": bool(row["pending"]),
        "abnormal": bool(row["abnormal"]),
        "version": row["version"],
    }
    for key in ("bound_site_id", "bound_site_no", "bound_domain"):
        idx = key
        if idx in row.keys():
            data[key] = row[idx]
    return data


# ----------------------------- 站点 -----------------------------

def list_sites(
    conn: sqlite3.Connection,
    *,
    keyword: str | None = None,
    status: str | None = None,
    page: int = 1,
    size: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    where: list[str] = []
    params: list[Any] = []
    if keyword:
        where.append("site_no LIKE ?")
        params.append(f"%{keyword}%")
    if status:
        where.append("status = ?")
        params.append(status)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    total = int(
        conn.execute(f"SELECT COUNT(*) AS c FROM sites {clause}", params).fetchone()["c"]
    )
    offset = max(page - 1, 0) * size
    rows = conn.execute(
        f"SELECT * FROM sites {clause} ORDER BY id LIMIT ? OFFSET ?",
        [*params, size, offset],
    ).fetchall()
    return [_site_row(row) for row in rows], total


def get_site(conn: sqlite3.Connection, site_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM sites WHERE id=?", (site_id,)).fetchone()
    return _site_row(row) if row else None


def get_site_by_no(conn: sqlite3.Connection, site_no: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM sites WHERE site_no=?", (site_no,)).fetchone()
    return _site_row(row) if row else None


def insert_site(conn: sqlite3.Connection, values: dict[str, Any]) -> dict[str, Any]:
    cur = conn.execute(
        "INSERT INTO sites(site_no, domain, site_type, request_method, charset,"
        " header_template, cookie_config, owner, status, pending, abnormal)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, '草稿', 1, 0)",
        (
            str(values["站点编号"]).strip(),
            str(values["域名地址"]).strip(),
            str(values["站点类型"]).strip(),
            values.get("请求方式"),
            values.get("字符编码"),
            values.get("请求头模板"),
            values.get("Cookie配置"),
            values.get("站点负责人"),
        ),
    )
    return get_site(conn, int(cur.lastrowid))  # type: ignore[return-value]


def update_site_state(
    conn: sqlite3.Connection,
    site_id: int,
    *,
    status: str,
    pending: bool,
    abnormal: bool,
    expected_version: int | None,
) -> dict[str, Any]:
    current = get_site(conn, site_id)
    if current is None:
        raise KeyError(f"采集站点 {site_id} 不存在或已归档")
    if expected_version is not None and int(expected_version) != int(current["version"]):
        raise ConflictError(
            f"站点 {current['站点编号']} 已被其他请求修改（当前版本 {current['version']}），请刷新后重试",
            current_version=int(current["version"]),
        )
    conn.execute(
        "UPDATE sites SET status=?, pending=?, abnormal=?, version=version+1,"
        " updated_at=datetime('now') WHERE id=?",
        (status, 1 if pending else 0, 1 if abnormal else 0, site_id),
    )
    return get_site(conn, site_id)  # type: ignore[return-value]


# ----------------------------- 凭证 -----------------------------

def list_credentials(
    conn: sqlite3.Connection,
    *,
    keyword: str | None = None,
    status: str | None = None,
    page: int = 1,
    size: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    where: list[str] = []
    params: list[Any] = []
    if keyword:
        where.append("c.credential_no LIKE ?")
        params.append(f"%{keyword}%")
    if status:
        where.append("c.status = ?")
        params.append(status)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    total = int(
        conn.execute(
            f"SELECT COUNT(*) AS c FROM credentials c {clause}", params
        ).fetchone()["c"]
    )
    offset = max(page - 1, 0) * size
    rows = conn.execute(
        "SELECT c.*, b.site_id AS bound_site_id, s.site_no AS bound_site_no,"
        " s.domain AS bound_domain"
        " FROM credentials c"
        " LEFT JOIN credential_bindings b ON b.credential_id=c.id"
        " LEFT JOIN sites s ON s.id=b.site_id"
        f" {clause} ORDER BY c.id LIMIT ? OFFSET ?",
        [*params, size, offset],
    ).fetchall()
    return [_credential_row(row) for row in rows], total


def get_credential(
    conn: sqlite3.Connection, credential_id: int, *, with_binding: bool = True
) -> dict[str, Any] | None:
    if with_binding:
        row = conn.execute(
            "SELECT c.*, b.site_id AS bound_site_id, s.site_no AS bound_site_no,"
            " s.domain AS bound_domain"
            " FROM credentials c"
            " LEFT JOIN credential_bindings b ON b.credential_id=c.id"
            " LEFT JOIN sites s ON s.id=b.site_id WHERE c.id=?",
            (credential_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM credentials WHERE id=?", (credential_id,)
        ).fetchone()
    return _credential_row(row) if row else None


def get_credential_by_no(
    conn: sqlite3.Connection, credential_no: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM credentials WHERE credential_no=?", (credential_no,)
    ).fetchone()
    return _credential_row(row) if row else None


def insert_credential(
    conn: sqlite3.Connection, values: dict[str, Any]
) -> dict[str, Any]:
    cur = conn.execute(
        "INSERT INTO credentials(credential_no, target_site, auth_method, account_id,"
        " valid_days, session_keep, refresh_trigger, expiry_alert, status, pending, abnormal)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, '草稿', 1, 0)",
        (
            str(values["凭证编号"]).strip(),
            values.get("目标站点"),
            str(values["认证方式"]).strip(),
            values.get("账号标识"),
            values.get("凭据有效天数"),
            values.get("会话保持"),
            values.get("刷新触发"),
            values.get("失效告警"),
        ),
    )
    return get_credential(conn, int(cur.lastrowid))  # type: ignore[return-value]


def update_credential_state(
    conn: sqlite3.Connection,
    credential_id: int,
    *,
    status: str,
    pending: bool,
    abnormal: bool,
    expected_version: int | None,
) -> dict[str, Any]:
    current = get_credential(conn, credential_id)
    if current is None:
        raise KeyError(f"凭证配置 {credential_id} 不存在或已归档")
    if expected_version is not None and int(expected_version) != int(current["version"]):
        raise ConflictError(
            f"凭证 {current['凭证编号']} 已被其他请求修改（当前版本 {current['version']}），请刷新后重试",
            current_version=int(current["version"]),
        )
    conn.execute(
        "UPDATE credentials SET status=?, pending=?, abnormal=?, version=version+1,"
        " updated_at=datetime('now') WHERE id=?",
        (status, 1 if pending else 0, 1 if abnormal else 0, credential_id),
    )
    return get_credential(conn, credential_id)  # type: ignore[return-value]


def bind_credential(
    conn: sqlite3.Connection,
    *,
    credential_id: int,
    site_id: int,
    expected_version: int | None,
) -> dict[str, Any]:
    """绑定或转交凭证：站点与凭证关系一起提交，冲突时整体回滚并回执。"""
    credential = get_credential(conn, credential_id)
    if credential is None:
        raise KeyError(f"凭证配置 {credential_id} 不存在或已归档")
    site = get_site(conn, site_id)
    if site is None:
        raise KeyError(f"采集站点 {site_id} 不存在或已归档，无法绑定")
    if (
        expected_version is not None
        and int(expected_version) != int(credential["version"])
    ):
        raise ConflictError(
            f"凭证 {credential['凭证编号']} 的绑定已被其他请求改动"
            f"（当前版本 {credential['version']}），本次转交未生效，请刷新后重试",
            current_version=int(credential["version"]),
        )

    existing = conn.execute(
        "SELECT site_id FROM credential_bindings WHERE credential_id=?",
        (credential_id,),
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO credential_bindings(credential_id, site_id) VALUES (?, ?)",
            (credential_id, site_id),
        )
    elif int(existing["site_id"]) != site_id:
        conn.execute(
            "UPDATE credential_bindings SET site_id=?, updated_at=datetime('now')"
            " WHERE credential_id=?",
            (site_id, credential_id),
        )
    conn.execute(
        "UPDATE credentials SET target_site=?, version=version+1,"
        " updated_at=datetime('now') WHERE id=?",
        (site["站点编号"], credential_id),
    )
    return get_credential(conn, credential_id)  # type: ignore[return-value]


def unbind_credential(
    conn: sqlite3.Connection,
    *,
    credential_id: int,
    expected_version: int | None,
) -> dict[str, Any]:
    credential = get_credential(conn, credential_id)
    if credential is None:
        raise KeyError(f"凭证配置 {credential_id} 不存在或已归档")
    if (
        expected_version is not None
        and int(expected_version) != int(credential["version"])
    ):
        raise ConflictError(
            f"凭证 {credential['凭证编号']} 已被其他请求修改（当前版本 {credential['version']}），请刷新后重试",
            current_version=int(credential["version"]),
        )
    conn.execute(
        "DELETE FROM credential_bindings WHERE credential_id=?", (credential_id,)
    )
    conn.execute(
        "UPDATE credentials SET target_site=NULL, status='草稿', pending=1,"
        " version=version+1, updated_at=datetime('now') WHERE id=?",
        (credential_id,),
    )
    return get_credential(conn, credential_id)  # type: ignore[return-value]


def list_migration_issues(
    conn: sqlite3.Connection, *, limit: int = 100
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, source, module, issue_type, business_no, message, created_at"
        " FROM migration_issues ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]
