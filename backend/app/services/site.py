"""站点管理业务规则：状态流转、字段校验、筛选口径与并发冲突回执。

数据已落到 SQLite（见 app.db / app.repositories），进程重启后登记记录仍在。
列表、明细、动作接口的入参与返回结构与内存时代保持一致，只多出 version 字段
用于并发修改时的冲突判定。
"""
from __future__ import annotations

from typing import Any

from app.db import ConflictError, db_conn, immediate_tx
from app import repositories as repo

MODULE = "site"
REQUIRED_FIELDS = ["站点编号", "域名地址", "站点类型"]
STATUS_ORDER = ["草稿", "待校验", "已激活", "已暂停"]
ACTION_RULES = {"提交校验": "待校验", "激活站点": "已激活", "暂停站点": "已暂停"}
NEGATIVE_ACTIONS = []


def _key_message(exc: KeyError) -> str:
    # KeyError 的 str 会给消息额外包一层引号，取 args 里的原文。
    return str(exc.args[0]) if exc.args else str(exc)


class SiteService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        with db_conn() as conn:
            return repo.list_sites(
                conn, keyword=keyword, status=status, page=page, size=size
            )

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        with db_conn() as conn:
            return repo.get_site(conn, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with db_conn() as conn, immediate_tx(conn):
            site_no = str(values["站点编号"]).strip()
            if repo.get_site_by_no(conn, site_no) is not None:
                raise ConflictError(
                    f"站点编号 {site_no} 已存在，登记被拒绝，未生成重复记录"
                )
            entry = repo.insert_site(conn, values)
        return entry, []

    def run_action(
        self, entry_id: int, action: str, *, expected_version: int | None = None
    ) -> tuple[dict[str, Any] | None, str]:
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于站点管理可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        try:
            with db_conn() as conn, immediate_tx(conn):
                entry = repo.update_site_state(
                    conn,
                    entry_id,
                    status=target,
                    pending=target != STATUS_ORDER[-1],
                    abnormal=action in NEGATIVE_ACTIONS,
                    expected_version=expected_version,
                )
        except KeyError as exc:
            return None, _key_message(exc)
        except ConflictError:
            raise
        return entry, f"采集站点已{action}"
