"""Tests for datensee.status — Dataflow job status polling.

Uses pytest-httpx to mock the Dataflow REST API.
"""

from __future__ import annotations

from pytest_httpx import HTTPXMock
from rich.table import Table

from datensee.status import (
    TERMINAL_STATES,
    JobInfo,
    JobState,
    _fetch_job_info,
    _parse_elapsed,
    _parse_metrics,
    _render_status_table,
    poll_job,
)

_BASE_URL = (
    "https://dataflow.googleapis.com/v1b3/projects/test-project/locations/us-central1/jobs/job-123"
)
_HEADERS = {"Authorization": "Bearer fake-token"}


class TestJobState:
    def test_terminal_states(self) -> None:
        assert JobState.DONE in TERMINAL_STATES
        assert JobState.FAILED in TERMINAL_STATES
        assert JobState.CANCELLED in TERMINAL_STATES
        assert JobState.RUNNING not in TERMINAL_STATES
        assert JobState.PENDING not in TERMINAL_STATES


class TestFetchJobInfo:
    def test_running_job(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            url=f"{_BASE_URL}?view=JOB_VIEW_ALL",
            json={
                "currentState": "JOB_STATE_RUNNING",
                "createTime": "2026-03-17T10:00:00Z",
            },
        )
        info = _fetch_job_info(_BASE_URL, _HEADERS)
        assert info.state == JobState.RUNNING
        assert info.elapsed_seconds is not None
        assert info.elapsed_seconds > 0

    def test_done_job(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            url=f"{_BASE_URL}?view=JOB_VIEW_ALL",
            json={"currentState": "JOB_STATE_DONE"},
        )
        info = _fetch_job_info(_BASE_URL, _HEADERS)
        assert info.state == JobState.DONE

    def test_http_error_returns_unknown(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            url=f"{_BASE_URL}?view=JOB_VIEW_ALL",
            status_code=500,
        )
        info = _fetch_job_info(_BASE_URL, _HEADERS)
        assert info.state == JobState.UNKNOWN

    def test_unknown_state_string(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            url=f"{_BASE_URL}?view=JOB_VIEW_ALL",
            json={"currentState": "JOB_STATE_BRAND_NEW"},
        )
        info = _fetch_job_info(_BASE_URL, _HEADERS)
        assert info.state == JobState.UNKNOWN

    def test_with_metrics(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            url=f"{_BASE_URL}?view=JOB_VIEW_ALL",
            json={
                "currentState": "JOB_STATE_RUNNING",
                "createTime": "2026-03-17T10:00:00Z",
                "jobMetrics": {
                    "metrics": [
                        {
                            "name": {
                                "name": "elements_produced",
                                "context": {"output_user_name": "fetch-tiles"},
                            },
                            "scalar": "42",
                        },
                        {
                            "name": {"name": "elements_added", "context": {}},
                            "scalar": "100",
                        },
                        {
                            "name": {"name": "current_num_workers", "context": {}},
                            "scalar": "5",
                        },
                    ]
                },
            },
        )
        info = _fetch_job_info(_BASE_URL, _HEADERS)
        assert info.elements_produced == 42
        assert info.elements_total == 100
        assert info.current_workers == 5


class TestParseElapsed:
    def test_valid_timestamp(self) -> None:
        elapsed = _parse_elapsed({"createTime": "2020-01-01T00:00:00Z"})
        assert elapsed is not None
        assert elapsed > 0

    def test_missing_timestamp(self) -> None:
        assert _parse_elapsed({}) is None

    def test_invalid_timestamp(self) -> None:
        assert _parse_elapsed({"createTime": "not-a-date"}) is None


class TestParseMetrics:
    def test_empty_metrics(self) -> None:
        produced, total, workers = _parse_metrics({})
        assert produced is None
        assert total is None
        assert workers is None

    def test_non_numeric_scalar_ignored(self) -> None:
        data = {
            "jobMetrics": {
                "metrics": [
                    {
                        "name": {"name": "current_num_workers", "context": {}},
                        "scalar": "not-a-number",
                    }
                ]
            }
        }
        _, _, workers = _parse_metrics(data)
        assert workers is None


class TestRenderStatusTable:
    def test_returns_table(self) -> None:
        info = JobInfo(state=JobState.RUNNING, elapsed_seconds=120.0)
        table = _render_status_table("job-123", info)
        assert isinstance(table, Table)

    def test_with_all_fields(self) -> None:
        info = JobInfo(
            state=JobState.RUNNING,
            elapsed_seconds=300.0,
            elements_produced=50,
            elements_total=100,
            current_workers=10,
        )
        table = _render_status_table("job-456", info)
        assert isinstance(table, Table)

    def test_unknown_state(self) -> None:
        info = JobInfo(state=JobState.UNKNOWN)
        table = _render_status_table("job-789", info)
        assert isinstance(table, Table)


class TestPollJob:
    def test_poll_reaches_terminal(self, httpx_mock: HTTPXMock) -> None:
        """Poll returns immediately when job is already done."""
        httpx_mock.add_response(
            url=f"{_BASE_URL}?view=JOB_VIEW_ALL",
            json={"currentState": "JOB_STATE_DONE"},
        )
        state = poll_job(
            "job-123",
            "test-project",
            "us-central1",
            "fake-token",
            poll_interval_seconds=0,
        )
        assert state == JobState.DONE

    def test_poll_transitions_to_done(self, httpx_mock: HTTPXMock) -> None:
        """Poll handles state transitions (running → done)."""
        httpx_mock.add_response(
            url=f"{_BASE_URL}?view=JOB_VIEW_ALL",
            json={"currentState": "JOB_STATE_RUNNING"},
        )
        httpx_mock.add_response(
            url=f"{_BASE_URL}?view=JOB_VIEW_ALL",
            json={"currentState": "JOB_STATE_DONE"},
        )
        state = poll_job(
            "job-123",
            "test-project",
            "us-central1",
            "fake-token",
            poll_interval_seconds=0,
        )
        assert state == JobState.DONE

    def test_poll_failed_job(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            url=f"{_BASE_URL}?view=JOB_VIEW_ALL",
            json={"currentState": "JOB_STATE_FAILED"},
        )
        state = poll_job(
            "job-123",
            "test-project",
            "us-central1",
            "fake-token",
            poll_interval_seconds=0,
        )
        assert state == JobState.FAILED
