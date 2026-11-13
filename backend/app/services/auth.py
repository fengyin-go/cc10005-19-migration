"""登录凭证业务规则：登记、状态流转、绑定/转交与并发冲突回执。

凭证与站点的绑定关系是正式数据：bind/unbind 在一个立即事务里同时修改
credentials 与 credential_bindings，任意一边写失败整个事务回滚。
目标站点不存在、版本号冲突时给出明确回执，绝不把凭证挂到错误站点上。
"""
from __future__ import annotations

from typing import Any

from app.db import ConflictError, db_conn, immediate_tx
from app import repositories as repo

MODULE = "auth"
REQUIRED_FIELDS = ["凭证编号", "认证方式"]
STATUS_ORDER = ["草稿", "待绑定", "已生效", "已失效"]
ACTION_RULES = {"提交绑定": "待绑定", "启用凭证": "已生效", "标记失效": "已失效"}
NEGATIVE_ACTIONS = []


def _key_message(exc: KeyError) -> str:
    # KeyError 的 str 会给消息额外包一层引号，取 args 里的原文。
    return str(exc.args[0]) if exc.args else str(exc)


class AuthService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        with db_conn() as conn:
            return repo.list_credentials(
                conn, keyword=keyword, status=status, page=page, size=size
            )

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        with db_conn() as conn:
            return repo.get_credential(conn, entry_id)

    def create_entry(
        self, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [
            field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()
        ]
        if missing:
            return None, missing
        with db_conn() as conn, immediate_tx(conn):
            credential_no = str(values["凭证编号"]).strip()
            if repo.get_credential_by_no(conn, credential_no) is not None:
                raise ConflictError(
                    f"凭证编号 {credential_no} 已存在，登记被拒绝，未生成重复记录"
                )
            entry = repo.insert_credential(conn, values)
        return entry, []

    def run_action(
        self, entry_id: int, action: str, *, expected_version: int | None = None
    ) -> tuple[dict[str, Any] | None, str]:
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于登录凭证可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        try:
            with db_conn() as conn, immediate_tx(conn):
                if action == "启用凭证":
                    credential = repo.get_credential(conn, entry_id)
                    if credential is None:
                        raise KeyError(f"凭证配置 {entry_id} 不存在或已归档")
                    if credential.get("bound_site_id") is None:
                        raise ConflictError(
                            f"凭证 {credential['凭证编号']} 尚未绑定有效站点，"
                            "不能启用，请先完成绑定"
                        )
                entry = repo.update_credential_state(
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
        return entry, f"凭证配置已{action}"

    def bind_entry(
        self,
        entry_id: int,
        site_id: int,
        *,
        expected_version: int | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """把凭证绑定到站点；已绑定其他站点时即为转交，整笔事务提交。"""
        try:
            with db_conn() as conn, immediate_tx(conn):
                current = repo.get_credential(conn, entry_id)
                if current is None:
                    raise KeyError(f"凭证配置 {entry_id} 不存在或已归档")
                transferring = current.get("bound_site_id") not in (None, site_id)
                entry = repo.bind_credential(
                    conn,
                    credential_id=entry_id,
                    site_id=site_id,
                    expected_version=expected_version,
                )
        except KeyError as exc:
            return None, _key_message(exc)
        except ConflictError:
            raise
        message = (
            f"凭证 {entry['凭证编号']} 已转交到站点 {entry['bound_site_no']}"
            if transferring
            else f"凭证 {entry['凭证编号']} 已绑定站点 {entry['bound_site_no']}"
        )
        return entry, message

    def unbind_entry(
        self, entry_id: int, *, expected_version: int | None = None
    ) -> tuple[dict[str, Any] | None, str]:
        try:
            with db_conn() as conn, immediate_tx(conn):
                entry = repo.unbind_credential(
                    conn,
                    credential_id=entry_id,
                    expected_version=expected_version,
                )
        except KeyError as exc:
            return None, _key_message(exc)
        except ConflictError:
            raise
        return entry, f"凭证 {entry['凭证编号']} 已解除站点绑定，状态回到草稿"

    def migration_issues(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with db_conn() as conn:
            return repo.list_migration_issues(conn, limit=limit)
