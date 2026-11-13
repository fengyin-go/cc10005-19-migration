#!/usr/bin/env python3
"""把进程内存时代的导出快照（JSON）迁移进 SQLite。

用法：
    .venv/bin/python import_legacy.py snapshot.json
    .venv/bin/python import_legacy.py snapshot.json --source 运维导出 --db-path data/app.sqlite3

快照格式与后端 /api/site/export、/api/auth/export 的结构对齐：
    {"site": [ {...行...} ], "auth": [ {...行...} ]}

重复业务编号、无法匹配目标站点的凭证不会被任意合并，问题会写入 migration_issues，
同一文件重复执行由内容哈希识别并跳过，升级中断后继续执行也不会重复装载。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.db import db_conn, immediate_tx, init_db
from app.legacy import ingest_snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description="历史内存数据迁移到 SQLite")
    parser.add_argument("snapshot", help="内存快照 JSON 文件路径")
    parser.add_argument("--source", default="legacy-import", help="迁移来源标记")
    parser.add_argument("--db-path", default=None, help="覆盖默认 SQLite 路径")
    args = parser.parse_args()

    snapshot_path = Path(args.snapshot)
    if not snapshot_path.is_file():
        print(f"快照文件不存在：{snapshot_path}", file=sys.stderr)
        return 2
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        print("快照顶层必须是 {site: [...], auth: [...]} 结构", file=sys.stderr)
        return 2

    init_db(args.db_path)
    with db_conn(args.db_path) as conn, immediate_tx(conn):
        report = ingest_snapshot(conn, payload, source=args.source)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
