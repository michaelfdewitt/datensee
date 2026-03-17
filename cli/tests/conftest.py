"""Shared test fixtures and pytest configuration."""

from __future__ import annotations


def pytest_addoption(parser: object) -> None:
    parser.addoption(
        "--integration",
        action="store_true",
        default=False,
        help="Run integration tests that hit the EE High Volume API.",
    )
    parser.addoption(
        "--gee-project",
        action="store",
        default=None,
        help="GCP project ID for EE integration tests.",
    )


def pytest_configure(config: object) -> None:
    config.addinivalue_line(
        "markers",
        "integration: marks tests that call the real EE High Volume API",
    )


def pytest_collection_modifyitems(config: object, items: list) -> None:
    if not config.getoption("--integration"):
        skip = __import__("pytest").mark.skip(
            reason="Integration tests require --integration flag"
        )
        for item in items:
            if "integration" in item.keywords:
                item.add_marker(skip)
