"""Verify provider validation, context-safe logging and lifecycle telemetry."""

import asyncio
import json
import logging
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pg_mcp.config.settings import OpenAIConfig, ValidationConfig
from pg_mcp.models.errors import LLMError, LLMTimeoutError, LLMUnavailableError
from pg_mcp.observability.logging import (
    JSONFormatter,
    RequestContextFilter,
    SensitiveDataFilter,
    TextFormatter,
    configure_logging,
)
from pg_mcp.observability.tracing import (
    clear_request_id,
    get_request_id,
    get_tracing_logger,
    request_context,
    set_request_id,
    trace_async,
    trace_sync,
)
from pg_mcp.services.result_validator import ResultValidator


@pytest.fixture
def validator():
    with patch("pg_mcp.services.result_validator.AsyncOpenAI") as client:
        client.return_value.chat.completions.create = AsyncMock()
        yield ResultValidator(OpenAIConfig(api_key="sk-test"), ValidationConfig(sample_rows=1))


def completion(content, tokens=17):
    response = MagicMock()
    response.choices = [SimpleNamespace(message=SimpleNamespace(content=content))]
    response.usage.total_tokens = tokens
    return response


@pytest.mark.parametrize(
    "confidence,expected", [(90, 90), (101, 100), (-5, 0), (85.4, 85), ("bad", 50)]
)
async def test_result_validation_scores_and_sampling(validator, confidence, expected):
    validator.client.chat.completions.create.return_value = completion(
        json.dumps(
            {
                "confidence": confidence,
                "explanation": "assessment",
            }
        )
    )
    result = await validator.validate("count", "SELECT id FROM users", [{"id": 1}, {"id": 2}], 2)
    assert result.confidence == expected
    assert result.is_acceptable == (expected >= 70)
    assert validator.get_tokens_used() == 17
    prompt = validator.client.chat.completions.create.call_args.kwargs["messages"][1]["content"]
    assert '"id": 1' in prompt
    assert '"id": 2' not in prompt


async def test_result_validation_uses_both_thresholds(validator):
    validator.validation_config.min_confidence_score = 95
    validator.client.chat.completions.create.return_value = completion('{"confidence":90}')
    result = await validator.validate("count", "SELECT 1", [], 0)
    assert not result.is_acceptable


async def test_result_validation_invalid_json(validator):
    validator.client.chat.completions.create.return_value = completion("bad json")
    result = await validator.validate("count", "SELECT 1", [], 0)
    assert not result.is_acceptable
    assert result.confidence == 60


@pytest.mark.parametrize("empty", ["choices", "content"])
async def test_empty_provider_response(validator, empty):
    response = completion("")
    if empty == "choices":
        response.choices = []
    validator.client.chat.completions.create.return_value = response
    with pytest.raises(LLMError):
        await validator.validate("count", "SELECT 1", [], 0)


@pytest.mark.parametrize(
    "error,expected,retryable",
    [
        (TimeoutError(), LLMTimeoutError, None),
        (RuntimeError("authentication failure"), LLMUnavailableError, False),
        (RuntimeError("rate_limit exceeded"), LLMUnavailableError, True),
        (RuntimeError("unexpected"), LLMError, False),
    ],
)
async def test_validation_provider_error_classification(validator, error, expected, retryable):
    validator.client.chat.completions.create.side_effect = error
    with pytest.raises(expected) as exc:
        await validator.validate("count", "SELECT 1", [], 0)
    if retryable is not None:
        assert exc.value.details["retryable"] is retryable


async def test_validation_disabled_does_not_call_provider(validator):
    validator.validation_config.enabled = False
    result = await validator.validate("count", "SELECT 1", [], 0)
    assert result.confidence == 100
    validator.client.chat.completions.create.assert_not_awaited()


async def test_trace_decorators_preserve_factory_and_isolate_concurrent_calls():
    factory = logging.getLogRecordFactory()

    @trace_sync("inner")
    def inner():
        return get_request_id()

    @trace_async("outer")
    async def outer():
        identifier = get_request_id()
        await asyncio.sleep(0)
        assert inner() == identifier
        return identifier

    identifiers = await asyncio.gather(outer(), outer())
    assert identifiers[0] != identifiers[1]
    assert get_request_id() is None
    assert logging.getLogRecordFactory() is factory
    assert inner() is not None
    assert get_request_id() is None


async def test_nested_trace_context_and_logger(caplog):
    logger = get_tracing_logger("test.trace")
    caplog.set_level(logging.DEBUG)
    set_request_id("parent")
    async with request_context("child"):
        for level in ("debug", "info", "warning", "error", "critical"):
            getattr(logger, level)("event", extra={"count": 1})
        try:
            raise ValueError("test")
        except ValueError:
            logger.exception("exception")
    assert get_request_id() == "parent"
    clear_request_id()
    assert all(r.request_id == "child" for r in caplog.records if r.name == "test.trace")


def test_log_redaction_and_json_serialization():
    record = logging.LogRecord(
        "test",
        logging.INFO,
        __file__,
        1,
        "event %s",
        ({"password": "secret", "safe": [{"api_key": "secret"}, 1]},),
        None,
    )
    record.api_key = "secret"
    record.metadata = {"auth": "secret", "nested": ({"token": "secret"},)}
    record.request_id = "trace"
    assert SensitiveDataFilter().filter(record)
    encoded = JSONFormatter().format(record)
    assert "secret" not in encoded
    assert "REDACTED" in encoded
    assert json.loads(encoded)["request_id"] == "trace"
    assert "trace" in TextFormatter().format(record)


async def test_request_context_filter_respects_explicit_id():
    async with request_context("task"):
        record = logging.LogRecord("test", logging.INFO, __file__, 1, "event", (), None)
        RequestContextFilter().filter(record)
        assert record.request_id == "task"
        record.request_id = "explicit"
        RequestContextFilter().filter(record)
        assert record.request_id == "explicit"


@pytest.mark.parametrize("format_name", ["text", "json"])
def test_logging_targets_stderr_to_preserve_mcp_protocol(format_name):
    root = logging.getLogger()
    old_handlers, old_level = root.handlers[:], root.level
    try:
        configure_logging(log_format=format_name)
        assert root.handlers[0].stream is sys.stderr
        assert any(isinstance(f, RequestContextFilter) for f in root.handlers[0].filters)
    finally:
        root.handlers = old_handlers
        root.setLevel(old_level)
