"""Shared test fixtures and pytest configuration."""

from __future__ import annotations

import time
from datetime import UTC, datetime

import httpx


def pytest_addoption(parser: object) -> None:
    parser.addoption(
        "--integration",
        action="store_true",
        default=False,
        help="Run integration tests that hit the EE High Volume API.",
    )
    parser.addoption(
        "--scale",
        action="store_true",
        default=False,
        help="Run large-scale Dataflow tests (costs real money).",
    )
    parser.addoption(
        "--gee-project",
        action="store",
        default=None,
        help="GCP project ID for EE integration tests.",
    )
    parser.addoption(
        "--gcs-bucket",
        action="store",
        default=None,
        help="GCS bucket name for scale test output (e.g. 'datensee-testing').",
    )
    parser.addoption(
        "--keep-output",
        action="store_true",
        default=False,
        help="Keep GCS output from scale tests instead of cleaning up.",
    )


def pytest_configure(config: object) -> None:
    config.addinivalue_line(
        "markers",
        "integration: marks tests that call the real EE High Volume API",
    )
    config.addinivalue_line(
        "markers",
        "scale: marks large-scale Dataflow tests that cost real money",
    )


def pytest_collection_modifyitems(config: object, items: list) -> None:
    skip_integration = not config.getoption("--integration")
    skip_scale = not config.getoption("--scale")

    for item in items:
        if skip_integration and "integration" in item.keywords:
            item.add_marker(
                __import__("pytest").mark.skip(
                    reason="Integration tests require --integration flag"
                )
            )
        if skip_scale and "scale" in item.keywords:
            item.add_marker(
                __import__("pytest").mark.skip(
                    reason="Scale tests require --scale flag (costs real money)"
                )
            )


# ---------------------------------------------------------------------------
# EECU usage tracking
# ---------------------------------------------------------------------------

_MONITORING_URL = (
    "https://monitoring.googleapis.com/v3/projects/{project}/timeSeries"
)


def _get_eecu_total(project: str, token: str, since: str, until: str) -> float:
    """Query Cloud Monitoring for total EECU-seconds in a time window."""
    with httpx.Client(timeout=15.0) as client:
        resp = client.get(
            _MONITORING_URL.format(project=project),
            params={
                "filter": 'metric.type = "earthengine.googleapis.com/project/cpu/usage_time"',
                "interval.startTime": since,
                "interval.endTime": until,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    if resp.status_code != 200:
        return -1.0
    data = resp.json()
    total = 0.0
    for ts in data.get("timeSeries", []):
        for pt in ts.get("points", []):
            val = pt["value"].get("doubleValue", pt["value"].get("int64Value", 0))
            total += float(val)
    return total


def _utc_iso(t: float) -> str:
    return datetime.fromtimestamp(t, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def pytest_sessionstart(session: object) -> None:
    """Record the start time for EECU measurement."""
    if session.config.getoption("--integration") and session.config.getoption("--gee-project"):
        # Cloud Monitoring has ~1-2 min lag, so record wall time and
        # we'll pad the window in sessionfinish.
        session._eecu_start_wall = time.time()


def pytest_sessionfinish(session: object, exitstatus: int) -> None:
    """Print EECU usage delta after integration tests complete."""
    start_wall = getattr(session, "_eecu_start_wall", None)
    if start_wall is None:
        return

    project = session.config.getoption("--gee-project")
    if not project:
        return

    end_wall = time.time()
    elapsed = end_wall - start_wall

    # Get a token for the monitoring API.
    try:
        from datensee.auth import get_access_token
        token = get_access_token(
            scopes=["https://www.googleapis.com/auth/monitoring.read"]
        )
    except Exception:
        return

    # Cloud Monitoring data has ~60-120s lag. We pad the window by 2 min
    # on each side to capture all data points from the test run.
    pad = 120
    since = _utc_iso(start_wall - pad)
    until = _utc_iso(end_wall + pad)

    eecu_seconds = _get_eecu_total(project, token, since, until)
    if eecu_seconds < 0:
        return

    # Print the report via the terminal writer (survives pytest output capture).
    tw = session.config.get_terminal_writer()
    tw.sep("=", "EECU usage report")
    tw.line(f"  Project:       {project}")
    tw.line(f"  Wall time:     {elapsed:.1f}s")
    tw.line(f"  EECU-seconds:  {eecu_seconds:.1f}")
    tw.line(f"  EECU-minutes:  {eecu_seconds / 60:.2f}")
    tw.line(f"  EECU-hours:    {eecu_seconds / 3600:.4f}")
    tw.sep("=")
