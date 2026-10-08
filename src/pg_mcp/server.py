"""MCP endpoint with explicit multi-database resource lifecycle."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from asyncpg import Pool
from mcp.server.fastmcp import FastMCP

from pg_mcp.cache.schema_cache import SchemaCache
from pg_mcp.config.settings import Settings
from pg_mcp.db.pool import close_pools, create_pool
from pg_mcp.models.errors import ErrorCode
from pg_mcp.models.query import ErrorDetail, QueryRequest, QueryResponse, ReturnType
from pg_mcp.observability.logging import configure_logging, get_logger
from pg_mcp.observability.metrics import MetricsCollector
from pg_mcp.resilience.circuit_breaker import CircuitBreaker
from pg_mcp.resilience.rate_limiter import MultiRateLimiter
from pg_mcp.services.orchestrator import QueryOrchestrator
from pg_mcp.services.result_validator import ResultValidator
from pg_mcp.services.sql_executor import SQLExecutor
from pg_mcp.services.sql_generator import SQLGenerator
from pg_mcp.services.sql_validator import SQLValidator

logger = get_logger(__name__)
_settings: Settings | None = None
_pools: dict[str, Pool] | None = None
_schema_cache: SchemaCache | None = None
_orchestrator: QueryOrchestrator | None = None
_metrics: MetricsCollector | None = None
_circuit_breaker: CircuitBreaker | None = None
_rate_limiter: MultiRateLimiter | None = None


@asynccontextmanager
async def lifespan(_app: FastMCP) -> AsyncIterator[None]:
    """Initialize every configured database and clean up even after partial startup."""
    global _settings, _pools, _schema_cache, _orchestrator, _metrics
    global _circuit_breaker, _rate_limiter
    _pools = {}
    _schema_cache = None
    _orchestrator = None
    metrics_server = None
    clients = []
    try:
        _settings = Settings()
        configure_logging(
            level=_settings.observability.log_level, log_format=_settings.observability.log_format
        )
        configs = _settings.configured_databases
        for alias, config in configs.items():
            _pools[alias] = await create_pool(config)
        _schema_cache = SchemaCache(_settings.cache)
        for alias, pool in _pools.items():
            await _schema_cache.load(alias, pool)
        if _settings.cache.enabled and _settings.cache.auto_refresh:
            await _schema_cache.start_auto_refresh(
                interval_minutes=_settings.cache.refresh_interval_minutes,
                pools=_pools,
            )
        _metrics = MetricsCollector()
        if _settings.observability.metrics_enabled:
            from prometheus_client import start_http_server

            metrics_server = start_http_server(
                _settings.observability.metrics_port, registry=_metrics.registry
            )
        generator = SQLGenerator(_settings.openai)
        clients.append(generator.client)
        result_validator = ResultValidator(_settings.openai, _settings.validation)
        clients.append(result_validator.client)
        validators = {alias: SQLValidator(_settings.security_for(alias)) for alias in configs}
        executors = {
            alias: SQLExecutor(_pools[alias], _settings.security_for(alias), config)
            for alias, config in configs.items()
        }
        _circuit_breaker = CircuitBreaker(
            failure_threshold=_settings.resilience.circuit_breaker_threshold,
            recovery_timeout=_settings.resilience.circuit_breaker_timeout,
        )
        _rate_limiter = MultiRateLimiter(
            query_limit=_settings.resilience.query_limit, llm_limit=_settings.resilience.llm_limit
        )
        _orchestrator = QueryOrchestrator(
            sql_generator=generator,
            sql_validator=next(iter(validators.values())),
            sql_executor=None,
            sql_executors=executors,
            sql_validators=validators,
            result_validator=result_validator,
            schema_cache=_schema_cache,
            pools=_pools,
            resilience_config=_settings.resilience,
            validation_config=_settings.validation,
            circuit_breaker=_circuit_breaker,
            rate_limiter=_rate_limiter,
            metrics=_metrics,
        )
        logger.info("Server ready", extra={"databases": list(configs)})
        yield
    finally:
        _orchestrator = None
        if _schema_cache is not None:
            try:
                await asyncio.wait_for(_schema_cache.stop_auto_refresh(), timeout=3.0)
            except Exception:
                logger.exception("Error stopping schema refresh")
        if _pools:
            try:
                await close_pools(_pools, timeout=5.0)
            except Exception:
                logger.exception("Error closing database pools")
        for client in clients:
            try:
                await client.close()
            except Exception:
                logger.exception("Error closing LLM client")
        if metrics_server is not None:
            httpd, thread = metrics_server
            await asyncio.to_thread(httpd.shutdown)
            httpd.server_close()
            await asyncio.to_thread(thread.join, 3.0)
        _settings = _pools = _schema_cache = _metrics = None
        _circuit_breaker = _rate_limiter = None
        logger.info("Server stopped")


mcp = FastMCP("pg-mcp", lifespan=lifespan)


def _error(code: str, message: str) -> dict[str, Any]:
    return QueryResponse(
        success=False, confidence=0, error=ErrorDetail(code=code, message=message)
    ).to_dict()


@mcp.tool()
async def query(
    question: str, database: str | None = None, return_type: str = "result"
) -> dict[str, Any]:
    """Ask a database question; specify a database alias when multiple are configured.

    return_type accepts 'sql' (validate and return SQL) or 'result' (execute read-only).
    Errors always use the same response model and include an integer tokens_used.
    """
    if _orchestrator is None:
        return _error("SERVER_NOT_INITIALIZED", "Server not initialized properly")
    if return_type not in ("sql", "result"):
        return _error("INVALID_PARAMETER", "return_type must be 'sql' or 'result'")
    try:
        request = QueryRequest(
            question=question, database=database, return_type=ReturnType(return_type)
        )
    except ValueError:
        return _error(ErrorCode.INVALID_REQUEST.value, "Invalid request parameters")
    try:
        return (await _orchestrator.execute_query(request)).to_dict()
    except Exception:
        logger.exception("Unexpected error in query tool")
        return _error(ErrorCode.INTERNAL_ERROR.value, "Internal server error")


if __name__ == "__main__":
    import anyio

    anyio.run(mcp.run_stdio_async)
