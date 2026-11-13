"""分布式网页采集调度系统 后端服务入口。

启动：uvicorn app.main:app --host 127.0.0.1 --port 8000
健康检查：GET /api/health
"""
from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.db import SCHEMA_VERSION, init_db
from app.routers import ROUTERS
from app.store import store

logger = logging.getLogger("app")

app = FastAPI(title="分布式网页采集调度系统", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for module in ROUTERS:
    app.include_router(module.router)


@app.on_event("startup")
def on_startup() -> None:
    """启动即建库/升级：首次部署装载样例，再次启动只核对版本，不覆盖用户数据。"""
    report = init_db()
    logger.info("SQLite 初始化完成：%s", report)


@app.get("/api/health")
def health() -> dict[str, object]:
    """健康检查：确认服务监听、SQLite 结构版本与内存模块均已就绪。"""
    return {
        "ok": True,
        "app": settings.app_name,
        "modules": len(store.module_names()),
        "storage": {
            "engine": "sqlite",
            "path": settings.db_path,
            "schema_version": SCHEMA_VERSION,
        },
    }


@app.get("/api/overview")
def overview() -> dict[str, object]:
    """运营概览：把各业务模块的待处理量汇总成看板卡片。"""
    return store.overview()
