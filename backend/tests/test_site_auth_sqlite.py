"""站点/凭证 SQLite 迁移与业务接口测试（仅用标准库，不引第三方依赖）。

运行：
    cd backend && APP_DB_PATH=... .venv/bin/python -m unittest discover -s tests -v
每个用例使用独立临时库，互不影响；覆盖：
- 首次建库装样例、再次启动不覆盖、中断续跑不重复装载、旧版本识别
- 重复业务编号 / 无法匹配目标站点的迁移问题留痕，且不任意合并绑定
- 站点与凭证同事务提交、乐观并发 409 回执、写锁竞争翻译
- 登记、绑定、转交、解绑、重启后核对的完整链路
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path


class DatabaseTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "case.sqlite3")
        os.environ["APP_DB_PATH"] = self.db_path
        # 延迟导入，确保读到本用例设置的 APP_DB_PATH。
        import app.config as config
        import app.db as db_mod
        import app.services.site as site_svc
        import app.services.auth as auth_svc
        import app.routers.site as site_router
        import app.routers.auth as auth_router

        self.config = config
        self.db = db_mod
        self.site_service = site_svc.SiteService()
        self.auth_service = auth_svc.AuthService()
        self.site_router = site_router
        self.auth_router = auth_router

    def tearDown(self) -> None:
        self.tmp.cleanup()
        os.environ.pop("APP_DB_PATH", None)

    # ---------- 迁移与样例装载 ----------

    def test_fresh_init_loads_seed_and_second_init_keeps_user_data(self) -> None:
        report = self.db.init_db()
        self.assertEqual(report["migrations_run"], [1, 2])
        sites, site_total = self.site_service.list_entries(size=200)
        creds, cred_total = self.auth_service.list_entries(size=200)
        self.assertEqual(site_total, 3)
        self.assertEqual(cred_total, 3)

        # 用户在首次部署后新增的数据。
        entry, missing = self.site_service.create_entry(
            {"站点编号": "SITE-9001", "域名地址": "example.com", "站点类型": "门户"}
        )
        self.assertEqual(missing, [])
        self.assertIsNotNone(entry)

        # 再次启动：不跑任何迁移、不重装样例、不覆盖用户数据。
        report_again = self.db.init_db()
        self.assertEqual(report_again["migrations_run"], [])
        sites, site_total = self.site_service.list_entries(size=200)
        self.assertEqual(site_total, 4)
        self.assertTrue(any(row["站点编号"] == "SITE-9001" for row in sites))
        with self.db.db_conn() as conn:
            batches = conn.execute("SELECT COUNT(*) AS c FROM import_batches").fetchone()["c"]
            self.assertEqual(batches, 1)
            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0],
                self.db.SCHEMA_VERSION,
            )

    def test_interrupted_migration_resumes_without_reloading_seed(self) -> None:
        original = dict(self.db.MIGRATIONS)

        def boom(conn: sqlite3.Connection) -> None:
            raise RuntimeError("模拟升级中断")

        # v1（含样例装载）先成功，v2 被人为打断。
        self.db.MIGRATIONS.clear()
        self.db.MIGRATIONS.update({1: original[1], 2: boom})
        with self.assertRaises(RuntimeError):
            self.db.init_db()

        with self.db.db_conn() as conn:
            applied = {row["version"] for row in conn.execute(
                "SELECT version FROM schema_migrations"
            )}
            self.assertEqual(applied, {1})
            site_count = conn.execute("SELECT COUNT(*) AS c FROM sites").fetchone()["c"]
            self.assertEqual(site_count, 3)

        # 恢复正常代码后继续升级：样例不重复装载。
        self.db.MIGRATIONS.clear()
        self.db.MIGRATIONS.update(original)
        report = self.db.init_db()
        self.assertEqual(report["migrations_run"], [2])
        with self.db.db_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) AS c FROM sites").fetchone()["c"], 3)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) AS c FROM credentials").fetchone()["c"], 3
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) AS c FROM import_batches").fetchone()["c"], 1
            )

    def test_database_newer_than_program_is_rejected(self) -> None:
        conn = self.db.connect()
        conn.execute("PRAGMA user_version=99")
        conn.close()
        with self.assertRaises(RuntimeError):
            self.db.init_db()

    def test_legacy_user_version_marks_history_without_rerun(self) -> None:
        # 旧库场景：结构只到 v1（用 user_version 标记），没有 schema_migrations 表，
        # 程序应把历史版本登记下来、只执行缺失的 v2，而不重跑 v1、不重装样例。
        conn = self.db.connect()
        conn.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY,"
            " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        conn.execute("BEGIN IMMEDIATE")
        self.db._migration_v1(conn)
        conn.execute("PRAGMA user_version=1")
        conn.execute("COMMIT")
        conn.close()
        with self.db.db_conn() as conn:
            self.assertIsNone(
                conn.execute(
                    "SELECT name FROM sqlite_master WHERE name='idx_sites_status'"
                ).fetchone()
            )

        report = self.db.init_db()
        self.assertNotIn(1, report["migrations_run"])
        self.assertEqual(report["migrations_run"], [2])
        # v1 装载的样例原样保留，没有重复装载。
        self.assertEqual(self.site_service.list_entries(size=200)[1], 3)

    # ---------- 迁移期数据质量识别 ----------

    def test_duplicate_business_numbers_are_recorded_not_merged(self) -> None:
        from app.legacy import ingest_snapshot

        payload = {
            "site": [
                {"id": 1, "站点编号": "SITE-DUP", "域名地址": "a", "站点类型": "t",
                 "status": "草稿", "pending": True, "abnormal": False},
                {"id": 2, "站点编号": "SITE-DUP", "域名地址": "b", "站点类型": "t",
                 "status": "草稿", "pending": True, "abnormal": False},
            ],
            "auth": [
                {"id": 1, "凭证编号": "AUTH-DUP", "认证方式": "账密",
                 "目标站点": "SITE-DUP", "status": "草稿", "pending": True, "abnormal": False},
                {"id": 2, "凭证编号": "AUTH-DUP", "认证方式": "账密",
                 "目标站点": "SITE-DUP", "status": "草稿", "pending": True, "abnormal": False},
            ],
        }
        self.db.init_db()
        with self.db.db_conn() as conn, self.db.immediate_tx(conn):
            report = ingest_snapshot(conn, payload, source="case-dup")
        self.assertEqual(report["sites_inserted"], 1)
        self.assertEqual(report["credentials_inserted"], 1)
        self.assertEqual(report["bindings_created"], 1)
        self.assertGreaterEqual(report["issues"], 2)

        # 同一份快照再导一次：哈希识别后整体跳过，不产生重复数据。
        with self.db.db_conn() as conn, self.db.immediate_tx(conn):
            again = ingest_snapshot(conn, payload, source="case-dup")
        self.assertTrue(again["skipped"])
        with self.db.db_conn() as conn:
            dup_sites = conn.execute(
                "SELECT COUNT(*) AS c FROM sites WHERE site_no='SITE-DUP'"
            ).fetchone()["c"]
            dup_creds = conn.execute(
                "SELECT COUNT(*) AS c FROM credentials WHERE credential_no='AUTH-DUP'"
            ).fetchone()["c"]
            self.assertEqual(dup_sites, 1)
            self.assertEqual(dup_creds, 1)

        issues = self.auth_service.migration_issues()
        kinds = {item["issue_type"] for item in issues}
        self.assertIn("DUPLICATE_SITE_NO", kinds)
        self.assertIn("DUPLICATE_CREDENTIAL_NO", kinds)

    def test_unmatched_target_site_creates_no_binding(self) -> None:
        from app.legacy import ingest_snapshot

        payload = {
            "site": [
                {"id": 1, "站点编号": "SITE-OK", "域名地址": "a", "站点类型": "t",
                 "status": "草稿", "pending": True, "abnormal": False},
            ],
            "auth": [
                {"id": 1, "凭证编号": "AUTH-LOOSE", "认证方式": "账密",
                 "目标站点": "SITE-GHOST", "status": "待绑定", "pending": True, "abnormal": False},
                {"id": 2, "凭证编号": "AUTH-OK", "认证方式": "账密",
                 "目标站点": "SITE-OK", "status": "草稿", "pending": True, "abnormal": False},
            ],
        }
        self.db.init_db()
        with self.db.db_conn() as conn, self.db.immediate_tx(conn):
            report = ingest_snapshot(conn, payload, source="case-unmatched")
        self.assertEqual(report["bindings_created"], 1)
        with self.db.db_conn() as conn:
            loose = conn.execute(
                "SELECT c.id FROM credentials c WHERE c.credential_no='AUTH-LOOSE'"
            ).fetchone()
            binding = conn.execute(
                "SELECT 1 FROM credential_bindings b JOIN credentials c ON c.id=b.credential_id"
                " WHERE c.credential_no='AUTH-LOOSE'"
            ).fetchone()
            self.assertIsNone(binding, "无法匹配目标站点时不允许产生绑定")

        creds, _ = self.auth_service.list_entries(keyword="AUTH-LOOSE", size=10)
        self.assertEqual(len(creds), 1)
        self.assertIsNone(creds[0]["bound_site_id"])
        issues = self.auth_service.migration_issues()
        self.assertTrue(
            any(i["issue_type"] == "TARGET_SITE_NOT_FOUND" and i["business_no"] == "AUTH-LOOSE"
                for i in issues)
        )

    def test_failed_ingest_rolls_back_everything(self) -> None:
        from app.legacy import ingest_snapshot

        payload = {
            "site": [
                {"id": 1, "站点编号": "SITE-TX", "域名地址": "a", "站点类型": "t",
                 "status": "草稿", "pending": True, "abnormal": False},
            ],
            "auth": [],
        }
        self.db.init_db()
        with self.assertRaises(sqlite3.Error):
            with self.db.db_conn() as conn, self.db.immediate_tx(conn):
                ingest_snapshot(conn, payload, source="case-rollback")
                # 人为制造一次写失败：违反唯一索引，整个事务必须回滚。
                conn.execute(
                    "INSERT INTO import_batches(source, content_hash, site_count,"
                    " credential_count, issue_count) VALUES (?, ?, 1, 0, 0)",
                    ("case-rollback", "not-the-hash"),
                )
                conn.execute(
                    "INSERT INTO import_batches(source, content_hash, site_count,"
                    " credential_count, issue_count) VALUES (?, ?, 1, 0, 0)",
                    ("case-rollback", "not-the-hash"),
                )
        with self.db.db_conn() as conn:
            self.assertIsNone(
                conn.execute(
                    "SELECT 1 FROM sites WHERE site_no='SITE-TX'"
                ).fetchone(),
                "事务失败后站点与批次写入都应撤销",
            )

    # ---------- 站点登记与动作语义 ----------

    def test_site_create_duplicate_and_required_fields(self) -> None:
        self.db.init_db()
        entry, missing = self.site_service.create_entry({"域名地址": "x"})
        self.assertIsNone(entry)
        self.assertIn("站点编号", missing)

        entry, missing = self.site_service.create_entry(
            {"站点编号": "SITE-1001", "域名地址": "d", "站点类型": "论坛"}
        )
        self.assertEqual(missing, [])
        self.assertEqual(entry["status"], "草稿")
        self.assertTrue(entry["pending"])
        self.assertEqual(entry["version"], 1)

        with self.assertRaises(self.db.ConflictError):
            self.site_service.create_entry(
                {"站点编号": "SITE-1001", "域名地址": "d2", "站点类型": "论坛"}
            )

    def test_site_list_filter_pagination_and_actions_keep_semantics(self) -> None:
        self.db.init_db()
        items, total = self.site_service.list_entries(keyword="SITE-0001")
        self.assertEqual(total, 1)
        self.assertEqual(items[0]["站点编号"], "SITE-0001")
        items, total = self.site_service.list_entries(status="草稿", size=2)
        self.assertEqual(total, 1)
        self.assertEqual(len(items), 1)
        items, total = self.site_service.list_entries(page=2, size=2)
        self.assertEqual(len(items), 1)

        entry, message = self.site_service.run_action(1, "提交校验")
        self.assertEqual(entry["status"], "待校验")
        self.assertEqual(entry["version"], 2)
        self.assertIn("提交校验", message)
        entry, _ = self.site_service.run_action(1, "激活站点")
        self.assertEqual(entry["status"], "已激活")
        # 与内存时代语义一致：只有走到最后一个状态「已暂停」pending 才为 False。
        self.assertTrue(entry["pending"])
        entry, _ = self.site_service.run_action(1, "暂停站点")
        self.assertEqual(entry["status"], "已暂停")
        self.assertFalse(entry["pending"])

        bad, message = self.site_service.run_action(1, "不存在的动作")
        self.assertIsNone(bad)
        self.assertIn("不属于", message)
        missing, message = self.site_service.run_action(9999, "激活站点")
        self.assertIsNone(missing)
        self.assertIn("不存在", message)

    def test_stale_version_returns_conflict_receipt(self) -> None:
        from fastapi import HTTPException

        self.db.init_db()
        # 第一次动作成功，版本从 1 升到 2。
        entry, _ = self.site_service.run_action(1, "提交校验")
        self.assertEqual(entry["version"], 2)
        # 拿着旧版本号再提交，服务端必须明确回执冲突。
        with self.assertRaises(self.db.ConflictError) as ctx:
            self.site_service.run_action(1, "激活站点", expected_version=1)
        self.assertEqual(ctx.exception.current_version, 2)

        # 路由层把冲突翻译成 409，并带回当前版本。
        from app.schemas import EntryPayload

        with self.assertRaises(HTTPException) as http_ctx:
            self.site_router.run_action(
                1, EntryPayload(values={"action": "激活站点"}, version=1)
            )
        self.assertEqual(http_ctx.exception.status_code, 409)
        self.assertEqual(http_ctx.exception.detail["current_version"], 2)

    def test_write_lock_is_translated_to_conflict(self) -> None:
        self.db.init_db()
        holder = self.db.connect()
        holder.execute("BEGIN IMMEDIATE")  # 占住写锁
        try:
            waiter = self.db.connect()
            waiter.execute("PRAGMA busy_timeout=300")
            with self.assertRaises(sqlite3.OperationalError) as ctx:
                waiter.execute("BEGIN IMMEDIATE")
            self.assertTrue(self.db.is_locked(ctx.exception))
        finally:
            holder.execute("ROLLBACK")
            holder.close()

    # ---------- 凭证绑定/转交/解绑 ----------

    def _create_site(self, no: str = "SITE-BIND") -> dict:
        entry, _ = self.site_service.create_entry(
            {"站点编号": no, "域名地址": f"{no}.test", "站点类型": "门户"}
        )
        return entry

    def _create_credential(self, no: str = "AUTH-BIND") -> dict:
        entry, _ = self.auth_service.create_entry(
            {"凭证编号": no, "认证方式": "账号密码"}
        )
        return entry

    def test_bind_transfer_unbind_full_flow(self) -> None:
        self.db.init_db()
        site_a = self._create_site("SITE-A")
        site_b = self._create_site("SITE-B")
        cred = self._create_credential("AUTH-X")

        bound, message = self.auth_service.bind_entry(cred["id"], site_a["id"])
        self.assertEqual(bound["bound_site_id"], site_a["id"])
        self.assertEqual(bound["目标站点"], "SITE-A")
        self.assertEqual(bound["version"], 2)
        self.assertIn("绑定", message)

        # 转交到另一个站点：仍是同一条凭证、同一条绑定关系。
        moved, message = self.auth_service.bind_entry(
            cred["id"], site_b["id"], expected_version=2
        )
        self.assertEqual(moved["bound_site_id"], site_b["id"])
        self.assertEqual(moved["version"], 3)
        self.assertIn("转交", message)
        with self.db.db_conn() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) AS c FROM credential_bindings WHERE credential_id=?",
                    (cred["id"],),
                ).fetchone()["c"],
                1,
                "转交是更新关系而不是新增一条绑定",
            )

        # 旧版本号转交必须收到冲突回执，关系保持不动。
        with self.assertRaises(self.db.ConflictError):
            self.auth_service.bind_entry(
                cred["id"], site_a["id"], expected_version=2
            )
        check, _ = self.auth_service.list_entries(keyword="AUTH-X")
        self.assertEqual(check[0]["bound_site_id"], site_b["id"])

        unbound, message = self.auth_service.unbind_entry(
            cred["id"], expected_version=3
        )
        self.assertIsNone(unbound["bound_site_id"])
        self.assertEqual(unbound["status"], "草稿")
        self.assertIn("解除", message)

    def test_bind_to_unknown_site_is_rejected(self) -> None:
        self.db.init_db()
        cred = self._create_credential()
        entry, message = self.auth_service.bind_entry(cred["id"], 9999)
        self.assertIsNone(entry)
        self.assertIn("不存在", message)
        # 凭证本身不能被半写：仍然没有绑定。
        fresh, _ = self.auth_service.list_entries(keyword="AUTH-BIND")
        self.assertIsNone(fresh[0]["bound_site_id"])

    def test_credential_requires_binding_before_enable(self) -> None:
        self.db.init_db()
        cred = self._create_credential()
        with self.assertRaises(self.db.ConflictError):
            self.auth_service.run_action(cred["id"], "启用凭证")
        site = self._create_site()
        bound, _ = self.auth_service.bind_entry(cred["id"], site["id"])
        enabled, message = self.auth_service.run_action(
            cred["id"], "启用凭证", expected_version=bound["version"]
        )
        self.assertEqual(enabled["status"], "已生效")
        self.assertIn("启用", message)
        # 与内存时代语义一致：只有最后一个状态「已失效」才把 pending 置 False。
        disabled, _ = self.auth_service.run_action(
            cred["id"], "标记失效", expected_version=enabled["version"]
        )
        self.assertEqual(disabled["status"], "已失效")
        self.assertFalse(disabled["pending"])

    def test_router_bind_endpoint_and_409_payload(self) -> None:
        from fastapi import HTTPException

        from app.schemas import BindingPayload

        self.db.init_db()
        site = self._create_site()
        cred = self._create_credential()
        result = self.auth_router.bind_entry(
            cred["id"], BindingPayload(site_id=site["id"])
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.entry["bound_site_no"], "SITE-BIND")

        with self.assertRaises(HTTPException) as ctx:
            self.auth_router.bind_entry(
                cred["id"], BindingPayload(site_id=site["id"], version=1)
            )
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("current_version", ctx.exception.detail)

    # ---------- 重启后核对 ----------

    def test_data_survives_across_connections(self) -> None:
        self.db.init_db()
        site = self._create_site("SITE-PERSIST")
        cred = self._create_credential("AUTH-PERSIST")
        self.auth_service.bind_entry(cred["id"], site["id"])

        # 模拟服务重启：丢掉服务实例，重新初始化并读取同一份数据库文件。
        import importlib

        import app.services.auth as auth_module
        import app.services.site as site_module
        importlib.reload(auth_module)
        importlib.reload(site_module)
        self.db.init_db()
        new_auth = auth_module.AuthService()
        new_site = site_module.SiteService()

        sites, total = new_site.list_entries(keyword="SITE-PERSIST")
        self.assertEqual(total, 1)
        creds, total = new_auth.list_entries(keyword="AUTH-PERSIST")
        self.assertEqual(total, 1)
        self.assertEqual(creds[0]["bound_site_id"], sites[0]["id"])
        self.assertEqual(creds[0]["bound_site_no"], "SITE-PERSIST")

    def test_cli_import_script_accepts_snapshot_file(self) -> None:
        import subprocess
        import sys

        self.db.init_db()
        snapshot = Path(self.tmp.name) / "snapshot.json"
        snapshot.write_text(
            json.dumps({
                "site": [
                    {"id": 1, "站点编号": "SITE-CLI", "域名地址": "cli", "站点类型": "t",
                     "status": "草稿", "pending": True, "abnormal": False}
                ],
                "auth": [
                    {"id": 1, "凭证编号": "AUTH-CLI", "认证方式": "token",
                     "目标站点": "SITE-CLI", "status": "草稿", "pending": True,
                     "abnormal": False}
                ],
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        backend_dir = Path(__file__).resolve().parents[1]
        env = dict(os.environ, APP_DB_PATH=self.db_path)
        proc = subprocess.run(
            [sys.executable, "import_legacy.py", str(snapshot), "--source", "cli-case"],
            cwd=backend_dir, env=env, capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["sites_inserted"], 1)
        self.assertEqual(report["bindings_created"], 1)

        # 再跑一次必须跳过。
        proc2 = subprocess.run(
            [sys.executable, "import_legacy.py", str(snapshot), "--source", "cli-case"],
            cwd=backend_dir, env=env, capture_output=True, text=True,
        )
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        self.assertTrue(json.loads(proc2.stdout)["skipped"])


if __name__ == "__main__":
    unittest.main()
