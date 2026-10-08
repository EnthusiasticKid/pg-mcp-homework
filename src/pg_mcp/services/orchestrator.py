"""Route and protect the complete natural-language query pipeline."""

import asyncio
import inspect
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import partial
from typing import Any, TypeVar

from asyncpg import Pool

from pg_mcp.cache.schema_cache import SchemaCache
from pg_mcp.config.settings import ResilienceConfig, ValidationConfig
from pg_mcp.models.errors import (
    DatabaseError,
    ErrorCode,
    LLMError,
    PgMcpError,
    RateLimitExceededError,
    SchemaLoadError,
    SecurityViolationError,
    SQLParseError,
)
from pg_mcp.models.query import (
    ErrorDetail,
    QueryRequest,
    QueryResponse,
    QueryResult,
    ReturnType,
    ValidationResult,
)
from pg_mcp.observability.metrics import MetricsCollector
from pg_mcp.observability.tracing import request_context
from pg_mcp.resilience.circuit_breaker import CircuitBreaker
from pg_mcp.resilience.rate_limiter import MultiRateLimiter, RateLimiter
from pg_mcp.services.result_validator import ResultValidator
from pg_mcp.services.sql_executor import SQLExecutor
from pg_mcp.services.sql_generator import SQLGenerator
from pg_mcp.services.sql_validator import SQLValidator

logger = logging.getLogger(__name__)
T = TypeVar("T")
_tokens: ContextVar[int] = ContextVar("query_tokens", default=0)


class QueryOrchestrator:
    """Coordinate database routing, security, resilience and request telemetry."""

    def __init__(
        self,
        sql_generator: SQLGenerator,
        sql_validator: SQLValidator,
        sql_executor: SQLExecutor | None,
        result_validator: ResultValidator,
        schema_cache: SchemaCache,
        pools: dict[str, Pool],
        resilience_config: ResilienceConfig,
        validation_config: ValidationConfig,
        *,
        sql_executors: dict[str, SQLExecutor] | None = None,
        sql_validators: dict[str, SQLValidator] | None = None,
        rate_limiter: MultiRateLimiter | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        metrics: MetricsCollector | None = None,
    ) -> None:
        self.sql_generator = sql_generator
        self.sql_validator = sql_validator
        self.sql_executor = sql_executor  # Compatibility for single-database callers.
        self.sql_executors = sql_executors or {}
        self.sql_validators = sql_validators or {}
        self.result_validator = result_validator
        self.schema_cache = schema_cache
        self.pools = pools
        self.resilience_config = resilience_config
        self.validation_config = validation_config
        self.circuit_breaker = circuit_breaker or CircuitBreaker(
            failure_threshold=resilience_config.circuit_breaker_threshold,
            recovery_timeout=resilience_config.circuit_breaker_timeout,
        )
        self.rate_limiter = rate_limiter or MultiRateLimiter(
            query_limit=resilience_config.query_limit,
            llm_limit=resilience_config.llm_limit,
        )
        self.metrics = metrics or MetricsCollector()

    @asynccontextmanager
    async def _limited(self, limiter: RateLimiter) -> AsyncIterator[None]:
        """Translate admission timeout without swallowing operation timeouts."""
        if not await limiter.acquire(timeout=self.resilience_config.acquire_timeout):
            raise RateLimitExceededError(
                "Concurrency limit exceeded",
                details={
                    "retry_after": self.resilience_config.acquire_timeout,
                },
            )
        try:
            yield
        finally:
            limiter.release()

    async def execute_query(self, request: QueryRequest) -> QueryResponse:
        """Return a structured result for every ordinary failure; propagate cancellation."""
        started = time.monotonic()
        database_name = "unresolved"
        status = "error"
        token = _tokens.set(0)
        async with request_context() as request_id:
            logger.info("Query started", extra={"request_id": request_id})
            try:
                if len(request.question) > self.validation_config.max_question_length:
                    raise PgMcpError(
                        "Question exceeds configured length limit", code=ErrorCode.QUESTION_TOO_LONG
                    )
                database_name = self._resolve_database(request.database)
                async with self._limited(self.rate_limiter.query_limiter):
                    response = await self._execute(request, database_name, request_id)
                status = "success"
            except PgMcpError as exc:
                response = QueryResponse(
                    success=False,
                    confidence=0,
                    error=ErrorDetail(
                        code=exc.code.value,
                        message=exc.message,
                        details=exc.details,
                    ),
                )
            except asyncio.CancelledError:
                status = "cancelled"
                raise
            except Exception:
                logger.exception("Unexpected query error", extra={"request_id": request_id})
                response = QueryResponse(
                    success=False,
                    confidence=0,
                    error=ErrorDetail(
                        code=ErrorCode.INTERNAL_ERROR.value,
                        message="Internal server error",
                    ),
                )
            finally:
                self.metrics.increment_query_request(status, database_name)
                self.metrics.query_duration.observe(time.monotonic() - started)
                logger.info(
                    "Query finished",
                    extra={
                        "request_id": request_id,
                        "database": database_name,
                        "status": status,
                    },
                )
                consumed = _tokens.get()
                _tokens.reset(token)
            response.request_id = request_id
            response.tokens_used = consumed
            return response

    async def _execute(
        self, request: QueryRequest, database: str, request_id: str
    ) -> QueryResponse:
        validator = self.sql_validators.get(database, self.sql_validator)
        # Never fall back to the primary executor for a multi-database request.
        executor = self.sql_executors.get(database)
        if executor is None and len(self.pools) == 1:
            executor = self.sql_executor
        if executor is None:
            raise DatabaseError(f"No executor configured for database '{database}'")
        schema = self.schema_cache.get(database)
        if schema is None:
            try:
                schema = await self.schema_cache.load(database, self.pools[database])
            except Exception as exc:
                raise SchemaLoadError(f"Failed to load schema for '{database}'") from exc
        age = self.schema_cache.get_cache_age(database)
        if isinstance(age, (int, float)):
            self.metrics.set_schema_cache_age(database, age)
        safe_schema = validator.filter_schema(schema)
        sql, validation, _ = await self._generate_sql_with_retry(
            request.question,
            safe_schema,
            request_id,
            validator=validator,
        )
        if request.return_type == ReturnType.SQL:
            return QueryResponse(success=True, generated_sql=sql, validation=validation)
        started = time.monotonic()
        try:
            rows, total = await self._execute_sql_with_retry(executor, sql)
        finally:
            self.metrics.observe_db_query_duration(time.monotonic() - started)
            pool = self.pools[database]
            if hasattr(pool, "get_size") and hasattr(pool, "get_idle_size"):
                active = pool.get_size() - pool.get_idle_size()
                if isinstance(active, int):
                    self.metrics.set_db_connections_active(database, active)
        elapsed_ms = (time.monotonic() - started) * 1000
        confidence = await self._validate_results_safely(
            request.question,
            sql,
            rows,
            total,
            request_id,
        )
        return QueryResponse(
            success=True,
            generated_sql=sql,
            validation=validation,
            confidence=confidence,
            confidence_acceptable=confidence
            >= max(
                self.validation_config.min_confidence_score,
                self.validation_config.confidence_threshold,
            ),
            data=QueryResult(
                columns=list(rows[0]) if rows else [], rows=rows, execution_time_ms=elapsed_ms
            ),
        )

    def _resolve_database(self, database: str | None) -> str:
        """Require an explicit alias when multiple databases are configured."""
        if database is not None:
            if database not in self.pools:
                raise DatabaseError(
                    f"Database '{database}' not found",
                    details={
                        "available_databases": list(self.pools),
                    },
                )
            return database
        if not self.pools:
            raise DatabaseError("No databases configured")
        if len(self.pools) == 1:
            return next(iter(self.pools))
        raise DatabaseError(
            "Multiple databases available, please specify which to query",
            details={"available_databases": list(self.pools)},
        )

    async def _backoff(self, attempt: int) -> None:
        await asyncio.sleep(
            self.resilience_config.retry_delay * self.resilience_config.backoff_factor**attempt
        )

    async def _call_llm(self, operation: str, call: Callable[[], Awaitable[T]], service: Any) -> T:
        """Protect each actual LLM call and retry only transient provider failures."""
        for attempt in range(self.resilience_config.max_retries + 1):
            async with self._limited(self.rate_limiter.llm_limiter):
                if not self.circuit_breaker.allow_request():
                    raise LLMError(
                        "LLM service temporarily unavailable (circuit breaker open)",
                        code=ErrorCode.LLM_UNAVAILABLE,
                    )
                started = time.monotonic()
                self.metrics.increment_llm_call(operation)
                try:
                    result = await call()
                except asyncio.CancelledError:
                    self.circuit_breaker.cancel_probe()
                    raise
                except LLMError as exc:
                    self.circuit_breaker.record_failure()
                    if not exc.details.get("retryable", exc.code == ErrorCode.LLM_TIMEOUT):
                        raise
                    if attempt >= self.resilience_config.max_retries:
                        raise
                except Exception as exc:
                    self.circuit_breaker.record_failure()
                    raise LLMError(
                        "LLM call failed unexpectedly",
                        details={
                            "error_type": type(exc).__name__,
                        },
                    ) from exc
                else:
                    self.circuit_breaker.record_success()
                    return result
                finally:
                    getter = getattr(service, "get_tokens_used", None)
                    used = getter() if callable(getter) else 0
                    if inspect.isawaitable(used):
                        used = await used
                    if isinstance(used, int):
                        _tokens.set(_tokens.get() + used)
                        self.metrics.increment_llm_tokens(operation, used)
                    self.metrics.observe_llm_latency(operation, time.monotonic() - started)
            await self._backoff(attempt)
        raise LLMError("LLM retries exhausted")  # pragma: no cover

    async def _generate_sql_with_retry(
        self,
        question: str,
        schema: Any,
        request_id: str,
        *,
        validator: SQLValidator | None = None,
    ) -> tuple[str, ValidationResult, int]:
        """Regenerate invalid SQL with feedback; service outages use separate retries."""
        validator = validator or self.sql_validator
        previous_sql = None
        feedback = None
        for attempt in range(self.resilience_config.max_retries + 1):
            sql = await self._call_llm(
                "generate_sql",
                partial(
                    self.sql_generator.generate,
                    question=question,
                    schema=schema,
                    previous_attempt=previous_sql,
                    error_feedback=feedback,
                ),
                self.sql_generator,
            )
            try:
                validator.validate_or_raise(sql)
            except (SecurityViolationError, SQLParseError) as exc:
                self.metrics.increment_sql_rejected(exc.code.value)
                if attempt >= self.resilience_config.max_retries:
                    raise
                previous_sql, feedback = sql, str(exc)
                await self._backoff(attempt)
            else:
                logger.info("SQL validated", extra={"request_id": request_id})
                return sql, ValidationResult(is_valid=True, is_select=True), _tokens.get()
        raise LLMError("SQL retries exhausted")  # pragma: no cover

    async def _execute_sql_with_retry(
        self, executor: SQLExecutor, sql: str
    ) -> tuple[list[dict[str, Any]], int]:
        """Retry read-only queries for explicit transient SQLSTATEs only."""
        for attempt in range(self.resilience_config.max_retries + 1):
            try:
                return await executor.execute(sql)
            except DatabaseError as exc:
                state = exc.details.get("error_code") or ""
                transient = state.startswith("08") or state in {"40001", "40P01", "57P03"}
                if not transient or attempt >= self.resilience_config.max_retries:
                    raise
                await self._backoff(attempt)
        raise DatabaseError("Database retries exhausted")  # pragma: no cover

    async def _validate_results_safely(
        self,
        question: str,
        sql: str,
        results: list[dict[str, Any]],
        row_count: int,
        request_id: str,
    ) -> int:
        """Keep data available if optional validation fails, with confidence zero."""
        if not self.validation_config.enabled:
            return 100
        try:
            async with asyncio.timeout(self.validation_config.timeout_seconds):
                result = await self._call_llm(
                    "validate_result",
                    lambda: self.result_validator.validate(
                        question=question,
                        sql=sql,
                        results=results,
                        row_count=row_count,
                    ),
                    self.result_validator,
                )
            return result.confidence
        except (LLMError, RateLimitExceededError, TimeoutError):
            logger.warning("Result validation unavailable", extra={"request_id": request_id})
            return 0

    @staticmethod
    def _get_current_time_ms() -> float:
        """Return monotonic milliseconds for elapsed-time measurements."""
        return time.monotonic() * 1000
