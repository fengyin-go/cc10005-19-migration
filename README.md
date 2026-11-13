# 分布式网页采集调度系统

面向多站点网页抽取、代理池调度、反爬对抗、增量采集、失败重试与采集结果校验的分布式采集调度系统。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   ├── app/db.py             SQLite 连接与版本化迁移
│   ├── app/repositories.py   站点/凭证 SQL 访问层
│   ├── app/legacy.py         内存快照迁移与重复/失联数据识别
│   ├── import_legacy.py      历史快照导入 CLI
│   ├── tests/                标准库 unittest 测试
│   └── app/store.py          其余模块的内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml      backend 的 SQLite 落在命名卷 sqlite_data
```

## SQLite 持久化（站点管理 + 登录凭证）

站点与登录凭证已从进程内存迁移到 SQLite，作为服务部署环境的正式存储：
登记、状态流转、凭证绑定/转交在服务重启后仍然可读。其余 16 个模块仍由内存仓库提供。

- 数据库路径由环境变量 `APP_DB_PATH` 控制，默认 `backend/data/app.sqlite3`；
  docker-compose 把它指向命名卷 `/data/app.sqlite3`。
- 结构升级走 `schema_migrations` 版本表 + `PRAGMA user_version`：每个版本在一个
  事务内完成，升级中断整体回滚，下次启动从断点继续，样例只在首次部署装载一次，
  再次启动不会覆盖用户数据。库版本高于程序版本时拒绝启动。
- 首次启动自动建库并装载内置样例。样例凭证的「目标站点」匹配不到任何站点编号，
  因此只登记凭证不建绑定，问题写入 `migration_issues`，可在登录凭证页面顶部或
  `GET /api/auth/migration-issues` 查看。
- 历史内存快照用 `backend/import_legacy.py 快照.json` 导入；重复业务编号保留先进入
  的一条、无法匹配目标站点的凭证不建立绑定，均登记到 `migration_issues`，绝不任意
  合并到一条有效绑定；同一快照按内容哈希去重，可安全重复执行。
- 站点与凭证的绑定关系与凭证本身在同一立即事务里提交，任一边写失败整体回滚。
- 每条站点/凭证带 `version`，动作与绑定接口支持乐观并发：携带过期版本会收到
  `409` 冲突回执（含服务端当前版本 `current_version`），写锁竞争也统一转成 409。

### 凭证绑定/转交接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/auth/{id}/bindings` | 请求体 `{site_id, version?}`；已绑他站即转交，冲突返回 409 |
| DELETE | `/api/auth/{id}/bindings` | 解绑，凭证回到草稿；可带 `{version}` 做冲突校验 |
| GET | `/api/auth/migration-issues` | 迁移遗留问题（重复编号、失联目标站点） |

### 测试

```bash
make test          # 后端 unittest（17 个用例）+ 前端类型检查/构建
# 或分开：
cd backend && .venv/bin/python -m unittest discover -s tests -v
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 站点管理 | `site` | 采集站点 | 站点编号、域名地址、站点类型 |
| 抽取模板 | `template` | 网页模板 | 模板编号、入口地址、列表选择器 |
| 代理池管理 | `proxy` | 代理节点 | 节点编号、代理地址、代理协议 |
| 采集任务 | `spider` | 采集作业 | 作业编号、目标站点、抽取模板 |
| 调度策略 | `schedule` | 调度规则 | 规则编号、关联站点、调度表达式 |
| 反爬对抗 | `antibot` | 对抗策略 | 策略编号、验证码类型、识别服务 |
| 限速控制 | `ratelimit` | 限速配置 | 配置编号、目标站点、每秒请求上限 |
| 失败重试 | `retry` | 重试策略 | 策略编号、关联任务、最大重试次数 |
| 字段映射 | `extractor` | 映射规则 | 规则编号、源字段名称、目标字段名称 |
| 采集流水线 | `pipeline` | 流水线步骤 | 步骤编号、步骤名称、输入数据源 |
| 结果校验 | `validator` | 校验规则 | 规则编号、校验对象、校验类型 |
| 数据落盘 | `storage` | 存储配置 | 配置编号、存储类型、输出路径 |
| 采集监控 | `monitor` | 监控指标 | 指标编号、关联任务、指标名称 |
| 数据清洗 | `cleanse` | 清洗规则 | 规则编号、目标字段、空值替换 |
| 增量采集 | `delta` | 增量游标 | 游标编号、目标站点、增量字段 |
| 数据分发 | `export` | 分发通道 | 通道编号、输出数据集、目标系统 |
| RPA流程 | `rpaflow` | 自动化流程 | 流程编号、目标页面、操作序列 |
| 登录凭证 | `auth` | 凭证配置 | 凭证编号、目标站点、认证方式 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据采集、爬虫与RPA
