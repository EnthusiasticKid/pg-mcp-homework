# PostgreSQL MCP：研发作业完成版

基于课程指定的 `tyrchen/geektime-bootcamp-ai/w5/pg-mcp`，补齐第五章第四节 Codex Review 指出的功能。实现与验证详见 [作业完成说明](HOMEWORK.md)。测试记录在 `test-results.xml`，覆盖率在 `coverage.json` 和 `htmlcov/index.html`。

实际运行截图见 [作业运行效果图](screenshots/README.md)。

## 安装与运行

支持 Python 3.12+；本次在 Windows / Python 3.13 中验证。依赖已写入 `uv.lock`，MCP 使用兼容课程接口的 1.x。

```powershell
# 在本项目目录运行。
uv sync --extra dev --locked
Copy-Item .env.example .env
# 编辑 .env，填入实际 PostgreSQL 连接信息与 API key。
uv run python -m pg_mcp
```

也可以使用 pip：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pg_mcp
```

`python main.py` 和 `pg-mcp` 命令都启动相同服务。服务使用 **stdio** 与 MCP 客户端通信；日志写入 stderr，避免混入协议输出。

## 配置与调用

本地 Qwen 等模型可通过 OpenAI 兼容接口连接：在 `.env` 中设置 `OPENAI_BASE_URL`（服务的 `/v1` 地址）和 `OPENAI_MODEL`（服务返回的实际模型 ID）。无鉴权的本地服务可设置 `OPENAI_API_KEY=sk-local`；有鉴权时填写服务要求的密钥，自定义接口不要求密钥以 `sk-` 开头。SQL 生成与结果校验使用同一接口。调用较慢时可调高 `OPENAI_TIMEOUT` 和 `VALIDATION_TIMEOUT_SECONDS`，最大为 120 秒和 60 秒。

单库模式保留 `DATABASE_*`。多库模式设置 `DATABASES` JSON：

```dotenv
DATABASES='{"analytics":{"host":"localhost","name":"reports","user":"reader","password":"replace"},"crm":{"host":"localhost","name":"customers","user":"reader","password":"replace"}}'
SECURITY_BLOCKED_TABLES=["private.audit"]
SECURITY_BLOCKED_COLUMNS=["password","api_key"]
SECURITY_ALLOW_EXPLAIN=false
RESILIENCE_QUERY_LIMIT=10
RESILIENCE_LLM_LIMIT=5
```

MCP 客户端调用 `query` 工具，参数示例：

```json
{"question":"统计用户数量","database":"crm","return_type":"result"}
```

- 单库时可以省略 `database`；多库时必须指定别名。
- `return_type="sql"` 只生成并校验 SQL；`result` 执行只读查询。
- `DATABASE_SECURITY` 可为别名单独配置策略；该配置替代该库的全局策略，未填写字段使用模型默认值。
- 字段黑名单采用保守匹配：`users.password` 同样阻止其他表的 `password` 字段；别名、CTE、子查询不能隐藏该字段。
- 有字段限制时禁止 `SELECT *`、`alias.*` 和整行引用；允许 `COUNT(*)`。
- 开启 EXPLAIN 仅允许普通 `EXPLAIN SELECT/WITH`，内部 SQL 必须通过安全校验；ANALYZE 和其他选项仍被拒绝。
- 限流是进程内的**并发数量控制**，不是按时间窗口计算 QPS。等待超时返回 `rate_limit_exceeded`。

数据库应使用具有实际表/列权限的只读账号。SQL 检查覆盖直接查询；视图与自定义函数可能间接访问对象，需通过 PostgreSQL GRANT/REVOKE 约束。行数限制沿用课程实现：取回后截断，不能据此保证大结果集的内存上限。

## 测试

```powershell
uv run pytest -p no:cacheprovider --cov=src --cov-report=term-missing --cov-fail-under=80
uv run ruff check src tests main.py
uv run mypy src --no-incremental
```

默认运行无需真实数据库或付费 API 的测试；依赖这些资源的课程集成/E2E 测试会明确跳过。配置 `.env` 后可执行：

```powershell
uv run pytest --live tests/integration tests/e2e -p no:cacheprovider
```

Prometheus 指标默认在 `http://localhost:9090/metrics`，覆盖查询成功/失败、耗时、LLM 调用/Token、SQL 拒绝、数据库耗时与缓存年龄。每个进入编排流程的请求都有独立 `request_id`。

Docker 文件已修正入口和依赖安装命令；本机没有 Docker，未进行镜像构建验证。仅需要本地测试数据库时可运行 `docker compose up -d postgres`。
