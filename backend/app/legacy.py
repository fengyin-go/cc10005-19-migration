"""历史数据（进程内存样例或导出快照）装载到 SQLite。

迁移纪律：
- 业务编号（站点编号 / 凭证编号）重复时保留先进入的一条，其余只登记问题，
  不会任意合并到某条有效记录上。
- 凭证的「目标站点」匹配不到任何站点编号时，凭证照常落库但不建立绑定，
  问题写入 migration_issues，等待人工在页面上转交，而不是猜一个站点绑过去。
- 整批装载在调用方的事务里执行；批次内容哈希写 import_batches，
  升级中断后重跑能识别同一批数据，不重复装载。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from app.seed import SEED_ROWS

ISSUE_DUPLICATE_SITE = "DUPLICATE_SITE_NO"
ISSUE_DUPLICATE_CREDENTIAL = "DUPLICATE_CREDENTIAL_NO"
ISSUE_TARGET_SITE_NOT_FOUND = "TARGET_SITE_NOT_FOUND"

SITE_TEXT_FIELDS = [
    "域名地址", "站点类型", "请求方式", "字符编码",
    "请求头模板", "Cookie配置", "站点负责人",
]
CREDENTIAL_TEXT_FIELDS = [
    "目标站点", "认证方式", "账号标识", "凭据有效天数",
    "会话保持", "刷新触发", "失效告警",
]


def _as_bool(value: Any) -> int:
    return 1 if value is True or value == 1 else 0


def _content_hash(payload: dict[str, list[dict[str, Any]]]) -> str:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _record_issue(
    conn: sqlite3.Connection,
    *,
    source: str,
    module: str,
    issue_type: str,
    business_no: str | None,
    message: str,
) -> None:
    conn.execute(
        "INSERT INTO migration_issues(source, module, issue_type, business_no, message)"
        " VALUES (?, ?, ?, ?, ?)",
        (source, module, issue_type, business_no, message),
    )


def ingest_snapshot(
    conn: sqlite3.Connection,
    payload: dict[str, list[dict[str, Any]]],
    *,
    source: str,
) -> dict[str, Any]:
    """把一份内存快照写进当前事务，返回装载与问题统计。"""
    content_hash = _content_hash(payload)
    existed = conn.execute(
        "SELECT 1 FROM import_batches WHERE source=? AND content_hash=?",
        (source, content_hash),
    ).fetchone()
    if existed is not None:
        return {
            "source": source,
            "skipped": True,
            "sites_inserted": 0,
            "credentials_inserted": 0,
            "bindings_created": 0,
            "issues": 0,
            "message": "同一批数据已经装载过，本次跳过，避免重复装载",
        }

    site_rows = payload.get("site", [])
    auth_rows = payload.get("auth", [])
    seen_site_no: set[str] = set()
    seen_credential_no: set[str] = set()
    issue_count = 0
    site_ids: dict[str, int] = {
        str(row["site_no"]): int(row["id"])
        for row in conn.execute("SELECT id, site_no FROM sites")
    }

    sites_inserted = 0
    for row in site_rows:
        site_no = str(row.get("站点编号") or "").strip()
        if not site_no:
            _record_issue(
                conn, source=source, module="site",
                issue_type=ISSUE_DUPLICATE_SITE, business_no=None,
                message="缺少站点编号，无法登记，已跳过该行",
            )
            issue_count += 1
            continue
        if site_no in seen_site_no or site_no in site_ids:
            _record_issue(
                conn, source=source, module="site",
                issue_type=ISSUE_DUPLICATE_SITE, business_no=site_no,
                message=f"站点编号 {site_no} 在本批或库内重复，保留先进入的一条，未合并后续记录",
            )
            issue_count += 1
            continue
        cur = conn.execute(
            "INSERT INTO sites(site_no, domain, site_type, request_method, charset,"
            " header_template, cookie_config, owner, status, pending, abnormal)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                site_no,
                str(row.get("域名地址") or ""),
                str(row.get("站点类型") or ""),
                row.get("请求方式"),
                row.get("字符编码"),
                row.get("请求头模板"),
                row.get("Cookie配置"),
                row.get("站点负责人"),
                str(row.get("status") or "草稿"),
                _as_bool(row.get("pending", True)),
                _as_bool(row.get("abnormal", False)),
            ),
        )
        site_ids[site_no] = int(cur.lastrowid)
        seen_site_no.add(site_no)
        sites_inserted += 1

    credentials_inserted = 0
    bindings_created = 0
    for row in auth_rows:
        credential_no = str(row.get("凭证编号") or "").strip()
        if not credential_no:
            _record_issue(
                conn, source=source, module="auth",
                issue_type=ISSUE_DUPLICATE_CREDENTIAL, business_no=None,
                message="缺少凭证编号，无法登记，已跳过该行",
            )
            issue_count += 1
            continue
        if credential_no in seen_credential_no:
            _record_issue(
                conn, source=source, module="auth",
                issue_type=ISSUE_DUPLICATE_CREDENTIAL, business_no=credential_no,
                message=f"凭证编号 {credential_no} 在本批重复，保留先进入的一条，未合并后续记录",
            )
            issue_count += 1
            continue
        target_site = str(row.get("目标站点") or "").strip() or None
        cur = conn.execute(
            "INSERT INTO credentials(credential_no, target_site, auth_method, account_id,"
            " valid_days, session_keep, refresh_trigger, expiry_alert, status,"
            " pending, abnormal)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                credential_no,
                target_site,
                str(row.get("认证方式") or ""),
                row.get("账号标识"),
                row.get("凭据有效天数"),
                row.get("会话保持"),
                row.get("刷新触发"),
                row.get("失效告警"),
                str(row.get("status") or "草稿"),
                _as_bool(row.get("pending", True)),
                _as_bool(row.get("abnormal", False)),
            ),
        )
        credential_id = int(cur.lastrowid)
        seen_credential_no.add(credential_no)
        credentials_inserted += 1

        matched_site_id = site_ids.get(target_site) if target_site else None
        if target_site and matched_site_id is None:
            _record_issue(
                conn, source=source, module="auth",
                issue_type=ISSUE_TARGET_SITE_NOT_FOUND, business_no=credential_no,
                message=(
                    f"凭证 {credential_no} 的目标站点「{target_site}」匹配不到任何站点编号，"
                    "未建立绑定，等待人工转交"
                ),
            )
            issue_count += 1
        elif matched_site_id is not None:
            conn.execute(
                "INSERT INTO credential_bindings(credential_id, site_id) VALUES (?, ?)",
                (credential_id, matched_site_id),
            )
            bindings_created += 1

    conn.execute(
        "INSERT INTO import_batches(source, content_hash, site_count,"
        " credential_count, issue_count) VALUES (?, ?, ?, ?, ?)",
        (source, content_hash, sites_inserted, credentials_inserted, issue_count),
    )
    return {
        "source": source,
        "skipped": False,
        "sites_inserted": sites_inserted,
        "credentials_inserted": credentials_inserted,
        "bindings_created": bindings_created,
        "issues": issue_count,
    }


def ingest_seed(conn: sqlite3.Connection) -> dict[str, Any]:
    """首次部署装载内置样例（只取 site / auth 两个模块，其余模块仍由内存仓库提供）。"""
    payload = {
        "site": SEED_ROWS["site"],
        "auth": SEED_ROWS["auth"],
    }
    return ingest_snapshot(conn, payload, source="seed")
