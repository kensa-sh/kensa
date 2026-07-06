from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest_plugins = ("pytester",)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-live",
        action="store_true",
        default=False,
        help="Run opt-in live tests using external services and provider API keys.",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "live: opt-in tests that call external services")
    config.addinivalue_line("markers", "openai: live OpenAI provider tests")
    config.addinivalue_line("markers", "anthropic: live Anthropic provider tests")
    if config.getoption("--run-live"):
        from dotenv import load_dotenv

        load_dotenv(Path.cwd() / ".env")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if not config.getoption("--run-live"):
        skip_live = pytest.mark.skip(reason="live tests require --run-live")
        for item in items:
            if "live" in item.keywords:
                item.add_marker(skip_live)
        return

    provider_skips = {
        "openai": ("OPENAI_API_KEY", pytest.mark.skip(reason="OPENAI_API_KEY is not set")),
        "anthropic": (
            "ANTHROPIC_API_KEY",
            pytest.mark.skip(reason="ANTHROPIC_API_KEY is not set"),
        ),
    }
    for item in items:
        if "live" in item.keywords:
            for marker_name, (env_name, skip_marker) in provider_skips.items():
                if marker_name in item.keywords and not os.environ.get(env_name):
                    item.add_marker(skip_marker)


@pytest.fixture(autouse=True)
def _configure_pytester_projects(request: pytest.FixtureRequest) -> None:
    if "pytester" not in request.fixturenames:
        return
    pytester = request.getfixturevalue("pytester")
    pytester.makeini(
        """
[pytest]
asyncio_default_fixture_loop_scope = function
"""
    )
