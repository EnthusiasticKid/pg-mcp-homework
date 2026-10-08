"""Pytest configuration and shared fixtures.

This module provides shared fixtures and configuration for all tests.
"""

import os

import pytest

from pg_mcp.config.settings import reset_settings


@pytest.fixture(autouse=True)
def reset_config() -> None:
    """Reset global settings before each test."""
    reset_settings()


@pytest.fixture(autouse=True)
def disable_metrics_for_tests():
    """Disable metrics for tests to avoid port conflicts."""
    os.environ["OBSERVABILITY_METRICS_ENABLED"] = "false"
    yield
    # Clean up
    if "OBSERVABILITY_METRICS_ENABLED" in os.environ:
        del os.environ["OBSERVABILITY_METRICS_ENABLED"]


def pytest_addoption(parser):
    """Real PostgreSQL/API tests must be explicitly selected."""
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="Run tests requiring a real PostgreSQL database and API key",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--live"):
        return
    skip = pytest.mark.skip(reason="Requires PostgreSQL/API credentials; run with --live")
    for item in items:
        if any(part in {"integration", "e2e"} for part in item.path.parts):
            item.add_marker(skip)
