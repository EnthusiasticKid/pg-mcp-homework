"""Regression tests for the three development homework requirements."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from prometheus_client import generate_latest
from pydantic import ValidationError

from pg_mcp.config.settings import (
    DatabaseConfig,
    OpenAIConfig,
    ResilienceConfig,
    SecurityConfig,
    Settings,
    ValidationConfig,
)
from pg_mcp.models.errors import DatabaseError, LLMError, LLMTimeoutError
from pg_mcp.models.query import ErrorDetail, QueryRequest, QueryResponse, QueryResult
from pg_mcp.models.schema import ColumnInfo, DatabaseSchema, TableInfo
from pg_mcp.observability.metrics import MetricsCollector
from pg_mcp.observability.tracing import get_request_id
from pg_mcp.resilience.circuit_breaker import CircuitBreaker
from pg_mcp.services.orchestrator import QueryOrchestrator
from pg_mcp.services.sql_executor import SQLExecutor
from pg_mcp.services.sql_generator import SQLGenerator
from pg_mcp.services.sql_validator import SQLValidator


@pytest.fixture
def schema():
    return DatabaseSchema(
        database_name="a",
        version="16",
        tables=[
            TableInfo(
                table_name="users",
                columns=[
                    ColumnInfo(name="id", data_type="integer", is_nullable=False),
                    ColumnInfo(name="password", data_type="text", is_nullable=True),
                ],
            ),
            TableInfo(table_name="secrets", columns=[]),
        ],
    )


@pytest.fixture
def flow(schema):
    generator = MagicMock(spec=SQLGenerator)
    generator.generate = AsyncMock(return_value="SELECT id FROM users")
    generator.get_tokens_used.return_value = 11
    cache = MagicMock()
    cache.get.return_value = schema
    cache.get_cache_age.return_value = 2.0
    executors = {name: MagicMock(spec=SQLExecutor) for name in ("a", "b")}
    for name, executor in executors.items():
        executor.execute = AsyncMock(return_value=([{"database": name}], 1))
    validator = SQLValidator(
        SecurityConfig(blocked_tables=["secrets"], blocked_columns=["users.password"])
    )
    return QueryOrchestrator(
        sql_generator=generator,
        sql_validator=validator,
        sql_executor=None,
        sql_executors=executors,
        sql_validators={"a": validator, "b": validator},
        result_validator=MagicMock(),
        schema_cache=cache,
        pools={"a": MagicMock(), "b": MagicMock()},
        resilience_config=ResilienceConfig(max_retries=2, retry_delay=0.1, acquire_timeout=0.01),
        validation_config=ValidationConfig(enabled=False),
        metrics=MetricsCollector(),
    )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM secrets",
        "SELECT * FROM public.secrets",
        'SELECT * FROM "public"."secrets"',
        "WITH s AS (SELECT id FROM secrets) SELECT id FROM s",
        "SELECT id FROM users UNION SELECT id FROM secrets",
        "SELECT u.password FROM users AS u",
        "SELECT password AS harmless FROM users",
        'SELECT u."password" FROM users u',
        "SELECT id FROM users WHERE password IS NOT NULL",
        "SELECT id FROM users ORDER BY password",
        "SELECT * FROM users",
        "SELECT u.* FROM users u",
        "SELECT row_to_json(u) FROM users u",
        "SELECT to_jsonb(u) FROM users u",
        "SELECT u FROM users u",
        "SELECT COUNT(u.*) FROM users u",
        "WITH u AS (SELECT password FROM users) SELECT * FROM u",
        "SELECT (SELECT password FROM users LIMIT 1)",
        "SELECT id FROM users UNION SELECT password FROM users",
        "WITH d AS (DELETE FROM users RETURNING id) SELECT id FROM d",
        "SELECT id INTO backup FROM users",
        "SELECT id FROM users FOR UPDATE",
        "SELECT 1; DELETE FROM users",
        "SELECT public.pg_sleep(1)",
    ],
)
def test_security_bypasses_rejected(sql):
    validator = SQLValidator(
        SecurityConfig(blocked_tables=["public.secrets"], blocked_columns=["users.password"])
    )
    assert not validator.validate(sql)[0]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM users",
        "SELECT COUNT(*) FROM users",
        "WITH u AS (SELECT id FROM users) SELECT id FROM u",
        "SELECT id FROM users UNION SELECT id FROM users",
        "SELECT id FROM (SELECT id FROM users UNION SELECT id FROM users) u",
    ],
)
def test_safe_queries_still_work(sql):
    assert SQLValidator(SecurityConfig(blocked_columns=["password"])).validate(sql)[0]


@pytest.mark.parametrize(
    "sql",
    [
        "EXPLAIN SELECT pg_sleep(1)",
        "EXPLAIN SELECT password FROM users",
        "EXPLAIN SELECT * FROM secrets",
        "EXPLAIN ANALYZE SELECT id FROM users",
        "EXPLAIN (ANALYZE TRUE) SELECT id FROM users",
        "EXPLAIN DELETE FROM users",
        "EXPLAIN WITH d AS (DELETE FROM users RETURNING id) SELECT id FROM d",
        "EXPLAIN SELECT id FROM users; DROP TABLE users",
    ],
)
def test_explain_cannot_bypass_security(sql):
    validator = SQLValidator(
        SecurityConfig(allow_explain=True, blocked_tables=["secrets"], blocked_columns=["password"])
    )
    assert not validator.validate(sql)[0]


def test_plain_explain_validated():
    assert SQLValidator(SecurityConfig(allow_explain=True)).validate("EXPLAIN SELECT 1")[0]
    assert not SQLValidator(SecurityConfig()).validate("EXPLAIN SELECT 1")[0]


@pytest.mark.parametrize(
    "sql",
    [
        "VACUUM users",
        "SELECT 1; VACUUM users",
        "SELECT query_to_xml('SELECT password FROM users',true,false,'')",
        "SELECT set_config('search_path','private',false)",
    ],
)
def test_commands_and_indirect_query_functions_rejected(sql):
    assert not SQLValidator(SecurityConfig()).validate(sql)[0]


def test_sql_extraction_preserves_explain_and_extra_statements():
    with patch("pg_mcp.services.sql_generator.AsyncOpenAI"):
        generator = SQLGenerator(OpenAIConfig(api_key="sk-test"))
    assert generator._extract_sql("EXPLAIN SELECT 1") == "EXPLAIN SELECT 1;"
    assert generator._extract_sql("SELECT 1; DELETE FROM users") == "SELECT 1; DELETE FROM users;"


def test_prompt_schema_does_not_leak_or_mutate(schema):
    safe = SQLValidator(
        SecurityConfig(blocked_tables=["secrets"], blocked_columns=["password"])
    ).filter_schema(schema)
    assert "password" not in safe.to_prompt_context()
    assert "secrets" not in safe.to_prompt_context()
    assert len(schema.tables) == 2
    assert len(schema.tables[0].columns) == 2


@pytest.mark.parametrize("target", ["a", "b"])
async def test_database_routes_to_its_own_executor(flow, target):
    response = await flow.execute_query(QueryRequest(question="ids", database=target))
    assert response.success
    assert response.data.rows == [{"database": target}]
    flow.sql_executors[target].execute.assert_awaited_once()
    other = "b" if target == "a" else "a"
    flow.sql_executors[other].execute.assert_not_awaited()
    assert response.tokens_used == 11
    assert response.request_id


@pytest.mark.parametrize("target", [None, "unknown"])
async def test_ambiguous_or_unknown_database_fails_before_llm(flow, target):
    response = await flow.execute_query(QueryRequest(question="ids", database=target))
    assert not response.success
    flow.sql_generator.generate.assert_not_awaited()


async def test_missing_executor_does_not_fall_back(flow):
    del flow.sql_executors["b"]
    flow.sql_executor = flow.sql_executors["a"]
    response = await flow.execute_query(QueryRequest(question="ids", database="b"))
    assert not response.success
    flow.sql_executor.execute.assert_not_awaited()


async def test_sql_only_does_not_execute(flow):
    response = await flow.execute_query(
        QueryRequest(question="ids", database="b", return_type="sql")
    )
    assert response.success and response.data is None
    flow.sql_executors["b"].execute.assert_not_awaited()


async def test_database_specific_policy(flow):
    flow.sql_validators["b"] = SQLValidator(SecurityConfig(blocked_tables=["users"]))
    with patch("pg_mcp.services.orchestrator.asyncio.sleep", new_callable=AsyncMock):
        response = await flow.execute_query(QueryRequest(question="ids", database="b"))
    assert response.error.code == "security_violation"
    flow.sql_executors["b"].execute.assert_not_awaited()
    assert response.tokens_used == 33


async def test_transient_llm_retries_backoff_and_token_accounting(flow):
    flow.sql_generator.get_tokens_used.side_effect = [0, 0, 11]
    flow.sql_generator.generate.side_effect = [
        LLMTimeoutError("timeout"),
        LLMTimeoutError("timeout"),
        "SELECT id FROM users",
    ]
    with patch("pg_mcp.services.orchestrator.asyncio.sleep", new_callable=AsyncMock) as sleep:
        response = await flow.execute_query(QueryRequest(question="ids", database="a"))
    assert response.success
    assert [c.args[0] for c in sleep.await_args_list] == [0.1, 0.2]
    assert flow.circuit_breaker.failure_count == 0
    assert response.tokens_used == 11


async def test_auth_failure_is_not_retried(flow):
    flow.sql_generator.generate.side_effect = LLMError("auth", details={"retryable": False})
    response = await flow.execute_query(QueryRequest(question="ids", database="a"))
    assert not response.success
    assert flow.sql_generator.generate.await_count == 1
    assert flow.circuit_breaker.failure_count == 1


async def test_open_circuit_stops_provider_calls(flow):
    flow.circuit_breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=60)
    flow.sql_generator.generate.side_effect = LLMTimeoutError("timeout")
    with patch("pg_mcp.services.orchestrator.asyncio.sleep", new_callable=AsyncMock):
        response = await flow.execute_query(QueryRequest(question="ids", database="a"))
    assert not response.success
    assert flow.sql_generator.generate.await_count == 1


@pytest.mark.parametrize("state,expected_calls", [("40001", 2), ("08006", 2), ("42601", 1)])
async def test_database_retry_classification(flow, state, expected_calls):
    flow.sql_executors["a"].execute.side_effect = [
        DatabaseError("db", details={"error_code": state}),
        ([{"id": 1}], 1),
    ]
    with patch("pg_mcp.services.orchestrator.asyncio.sleep", new_callable=AsyncMock):
        response = await flow.execute_query(QueryRequest(question="ids", database="a"))
    assert response.success == (expected_calls == 2)
    assert flow.sql_executors["a"].execute.await_count == expected_calls


async def test_admission_limit_and_cancellation_release(flow):
    flow.rate_limiter.query_limiter._max_concurrent = 1
    flow.rate_limiter.query_limiter._semaphore = asyncio.Semaphore(1)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_generate(**kwargs):
        entered.set()
        await release.wait()
        return "SELECT id FROM users"

    flow.sql_generator.generate.side_effect = slow_generate
    task = asyncio.create_task(flow.execute_query(QueryRequest(question="ids", database="a")))
    await entered.wait()
    rejected = await flow.execute_query(QueryRequest(question="ids", database="a"))
    assert rejected.error.code == "rate_limit_exceeded"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert flow.rate_limiter.query_limiter.active_count == 0
    assert flow.rate_limiter.llm_limiter.active_count == 0
    release.set()
    assert (await flow.execute_query(QueryRequest(question="ids", database="a"))).success


async def test_trace_ids_are_task_local_and_reset(flow):
    seen = []

    async def generate(**kwargs):
        seen.append(get_request_id())
        await asyncio.sleep(0)
        assert seen.count(get_request_id()) == 1
        return "SELECT id FROM users"

    flow.sql_generator.generate.side_effect = generate
    responses = await asyncio.gather(
        *[flow.execute_query(QueryRequest(question="ids", database=db)) for db in ("a", "b")]
    )
    assert {r.request_id for r in responses} == set(seen)
    assert get_request_id() is None


async def test_request_metrics_include_success_failure_security_and_tokens(flow):
    await flow.execute_query(QueryRequest(question="ids", database="a"))
    await flow.execute_query(QueryRequest(question="ids", database="unknown"))
    flow.resilience_config.max_retries = 0
    flow.sql_generator.generate.return_value = "SELECT password FROM users"
    await flow.execute_query(QueryRequest(question="password", database="a"))
    output = generate_latest(flow.metrics.registry).decode()
    assert 'status="success"} 1.0' in output
    assert 'status="error"} 1.0' in output
    assert 'reason="security_violation"} 1.0' in output
    assert 'operation="generate_sql"} 22.0' in output


async def test_configured_question_length(flow):
    flow.validation_config.max_question_length = 2
    response = await flow.execute_query(QueryRequest(question="three", database="a"))
    assert response.error.code == "question_too_long"
    flow.sql_generator.generate.assert_not_awaited()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"success": False},
        {"success": False, "error": None},
        {"success": True, "error": ErrorDetail(code="bad", message="bad")},
        {"success": False, "error": ErrorDetail(code="bad", message="bad"), "data": QueryResult()},
    ],
)
def test_response_invariants(kwargs):
    with pytest.raises(ValidationError):
        QueryResponse(**kwargs)


@pytest.mark.parametrize("tokens", [None, 0, 12])
def test_response_tokens_stable(tokens):
    response = QueryResponse(success=True, tokens_used=tokens)
    assert response.to_dict()["tokens_used"] == (tokens or 0)
    json.dumps(response.to_dict())


def test_row_count_derived_when_omitted():
    assert QueryResult(rows=[{"id": 1}]).row_count == 1
    assert QueryResult(rows=[{"id": 1}], row_count=99).row_count == 1


def test_multiple_database_settings_from_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv(
        "DATABASES", json.dumps({"analytics": {"name": "reports"}, "crm": {"name": "customers"}})
    )
    settings = Settings()
    assert set(settings.configured_databases) == {"analytics", "crm"}
    assert settings.configured_databases["crm"].name == "customers"


def test_flat_dotenv_settings_and_json_lists(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "OPENAI_API_KEY=sk-test\nDATABASE_NAME=dotenv_db\nRESILIENCE_QUERY_LIMIT=2\n"
        'SECURITY_BLOCKED_FUNCTIONS=["pg_sleep","custom_function"]\n'
        'SECURITY_BLOCKED_COLUMNS=["password"]\n',
        encoding="utf-8",
    )
    settings = Settings()
    assert settings.database.name == "dotenv_db"
    assert settings.openai.api_key.get_secret_value() == "sk-test"
    assert settings.resilience.query_limit == 2
    assert settings.security.blocked_functions == ["pg_sleep", "custom_function"]
    assert settings.security.blocked_columns == ["password"]


def test_unknown_policy_rejected():
    with pytest.raises(ValidationError, match="unknown databases"):
        Settings(
            openai=OpenAIConfig(api_key="sk-test"), database_security={"missing": SecurityConfig()}
        )


def test_pool_bound_validation():
    with pytest.raises(ValidationError):
        DatabaseConfig(min_pool_size=20, max_pool_size=5)


def test_half_open_allows_only_one_probe():
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=0)
    breaker.record_failure()
    assert breaker.allow_request()
    assert not breaker.allow_request()
    breaker.cancel_probe()
    assert breaker.allow_request()
    breaker.record_success()
    assert breaker.allow_request()


async def test_metrics_registry_can_be_reset():
    metrics = MetricsCollector()
    metrics.increment_query_request("success", "a")
    metrics.reset_all_metrics()
    assert b'database="a"' not in generate_latest(metrics.registry)


async def test_mcp_tool_errors_use_same_model(monkeypatch):
    import pg_mcp.server as server

    monkeypatch.setattr(server, "_orchestrator", None)
    result = await server.query("ids")
    assert result["tokens_used"] == 0
    monkeypatch.setattr(server, "_orchestrator", MagicMock())
    assert (await server.query("ids", return_type="bad"))["tokens_used"] == 0
    assert (await server.query(" "))["error"]["code"] == "invalid_request"


async def test_lifespan_creates_all_pools_and_closes_resources(monkeypatch, schema):
    import pg_mcp.server as server

    settings = Settings(
        openai=OpenAIConfig(api_key="sk-test"),
        databases={"a": DatabaseConfig(name="a"), "b": DatabaseConfig(name="b")},
    )
    settings.observability.metrics_enabled = False
    pools = [MagicMock(), MagicMock()]
    create = AsyncMock(side_effect=pools)
    close = AsyncMock()
    cache = MagicMock()
    cache.load = AsyncMock(return_value=schema)
    cache.stop_auto_refresh = AsyncMock()
    generator = MagicMock()
    generator.client.close = AsyncMock()
    validator = MagicMock()
    validator.client.close = AsyncMock()
    monkeypatch.setattr(server, "Settings", lambda: settings)
    monkeypatch.setattr(server, "configure_logging", MagicMock())
    monkeypatch.setattr(server, "create_pool", create)
    monkeypatch.setattr(server, "close_pools", close)
    monkeypatch.setattr(server, "SchemaCache", lambda _: cache)
    monkeypatch.setattr(server, "SQLGenerator", lambda _: generator)
    monkeypatch.setattr(server, "ResultValidator", lambda *_: validator)
    async with server.lifespan(server.mcp):
        assert set(server._orchestrator.sql_executors) == {"a", "b"}
        assert server._orchestrator.sql_executors["b"].pool is pools[1]
        assert server._orchestrator.rate_limiter is server._rate_limiter
        assert server._orchestrator.circuit_breaker is server._circuit_breaker
    assert create.await_count == 2
    close.assert_awaited_once()
    generator.client.close.assert_awaited_once()
    validator.client.close.assert_awaited_once()
    assert server._orchestrator is None
    assert server._pools is None


async def test_partial_startup_closes_previously_created_pools(monkeypatch):
    import pg_mcp.server as server

    settings = Settings(
        openai=OpenAIConfig(api_key="sk-test"),
        databases={"a": DatabaseConfig(name="a"), "b": DatabaseConfig(name="b")},
    )
    first_pool = MagicMock()
    close = AsyncMock()
    monkeypatch.setattr(server, "Settings", lambda: settings)
    monkeypatch.setattr(server, "configure_logging", MagicMock())
    monkeypatch.setattr(
        server, "create_pool", AsyncMock(side_effect=[first_pool, RuntimeError("db")])
    )
    monkeypatch.setattr(server, "close_pools", close)
    with pytest.raises(RuntimeError):
        async with server.lifespan(server.mcp):
            pass
    close.assert_awaited_once_with({"a": first_pool}, timeout=5.0)
    assert server._orchestrator is None
