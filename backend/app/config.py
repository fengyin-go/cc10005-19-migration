"""运行配置：端口、跨域、运行环境与 SQLite 存储位置。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _default_db_path() -> str:
    # 服务部署时用 APP_DB_PATH 指向挂载卷；本地开发默认落在 backend/data 下，
    # 该路径已被 .gitignore 的 *.sqlite3 规则忽略。
    return os.environ.get("APP_DB_PATH", "data/app.sqlite3")


@dataclass(frozen=True)
class Settings:
    app_name: str = "分布式网页采集调度系统"
    env: str = "local"
    port: int = 8000
    db_path: str = field(default_factory=_default_db_path)
    allowed_origins: list[str] = field(
        default_factory=lambda: [
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ]
    )
    page_size_default: int = 20
    page_size_max: int = 200


settings = Settings()
