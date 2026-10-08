# 作业运行效果图

两张图片均截自本地 MCP Inspector 的真实运行界面。

1. [MCP 服务连接成功](01-mcp-connected.png)：展示 `pg-mcp` 服务连接状态、协议及初始化调用。
2. [自然语言查询结果](02-query-result.png)：通过本地 Qwen3.6-27B 生成 SQL，查询 PostgreSQL 的 `xzqz` schema 下普通表的数量，不读取业务记录。

截图不包含模型密钥或数据库密码。MCP Inspector 是调试客户端，项目本身是 MCP 后端服务。

本次查询返回 `success: true` 和 `table_count: 11`；`confidence_acceptable: false` 表示模型结果复核未获得可接受的置信度，不代表结果复核通过。
