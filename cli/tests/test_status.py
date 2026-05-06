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
        m = _parse_metrics({})
        assert m.elements_produced is None
        assert m.elements_total is None
        assert m.current_workers is None
        assert m.failures_written is None
        assert m.output_tiles_written is None

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
        m = _parse_metrics(data)
        assert m.current_workers is None

    def test_datensee_user_counters(self) -> None:
        data = {
            "jobMetrics": {
                "metrics": [
                    {
                        "name": {
                            "name": "failures_written",
                            "context": {
                                "namespace": "datensee",
                                "step": "FailedTileWriter",
                            },
                        },
                        "scalar": "13",
                    },
                    {
                        "name": {
                            "name": "output_tiles_written",
                            "context": {"namespace": "datensee"},
                        },
                        "scalar": "7",
                    },
                ]
            }
        }
        m = _parse_metrics(data)
        assert m.failures_written == 13
        assert m.output_tiles_written == 7


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


class TestWatchdog:
    """The watchdog policies are pure-state functions; we exercise
    ``_watchdog_check`` directly so the tests don't have to spin up a
    fake polling loop."""

    def _state(self, started_seconds_ago: float = 0.0):
        from datensee.status import _WatchdogState

        s = _WatchdogState.fresh()
        # Adjust started_at backwards by N seconds so the check thinks
        # the job has been running for a while.
        s.started_at -= started_seconds_ago
        s.last_progress_at -= started_seconds_ago
        return s

    def test_max_runtime_breach(self) -> None:
        from datetime import timedelta

        from datensee.status import WatchdogConfig, _watchdog_check

        cfg = WatchdogConfig(
            max_runtime=timedelta(minutes=5),
            max_failure_rate=None,
            idle_timeout=None,
        )
        state = self._state(started_seconds_ago=400.0)  # 6m40s elapsed
        info = JobInfo(state=JobState.RUNNING)
        reason = _watchdog_check(cfg, state, info, tile_count=10)
        assert reason is not None
        assert "max_runtime" in reason

    def test_max_runtime_under_threshold_passes(self) -> None:
        from datetime import timedelta

        from datensee.status import WatchdogConfig, _watchdog_check

        cfg = WatchdogConfig(
            max_runtime=timedelta(hours=12),
            max_failure_rate=None,
            idle_timeout=None,
        )
        state = self._state(started_seconds_ago=120.0)
        info = JobInfo(state=JobState.RUNNING)
        assert _watchdog_check(cfg, state, info, tile_count=10) is None

    def test_max_runtime_disabled_when_none(self) -> None:
        from datensee.status import WatchdogConfig, _watchdog_check

        cfg = WatchdogConfig(
            max_runtime=None, max_failure_rate=None, idle_timeout=None
        )
        state = self._state(started_seconds_ago=10_000_000.0)
        info = JobInfo(state=JobState.RUNNING)
        assert _watchdog_check(cfg, state, info, tile_count=10) is None

    def test_failure_rate_breach_after_grace(self) -> None:
        from datetime import timedelta

        from datensee.status import WatchdogConfig, _watchdog_check

        cfg = WatchdogConfig(
            max_runtime=None,
            max_failure_rate=0.5,
            failure_grace_period=timedelta(minutes=5),
            idle_timeout=None,
        )
        state = self._state(started_seconds_ago=600.0)  # 10m, past grace
        info = JobInfo(state=JobState.RUNNING, failures_written=80)
        reason = _watchdog_check(cfg, state, info, tile_count=100)
        assert reason is not None
        assert "max_failure_rate" in reason

    def test_failure_rate_skipped_during_grace(self) -> None:
        from datetime import timedelta

        from datensee.status import WatchdogConfig, _watchdog_check

        cfg = WatchdogConfig(
            max_runtime=None,
            max_failure_rate=0.5,
            failure_grace_period=timedelta(minutes=10),
            idle_timeout=None,
        )
        state = self._state(started_seconds_ago=120.0)  # 2m, in grace
        info = JobInfo(state=JobState.RUNNING, failures_written=80)
        # In grace → no firing even at 80% failure rate.
        assert _watchdog_check(cfg, state, info, tile_count=100) is None

    def test_failure_rate_skipped_without_tile_count(self) -> None:
        from datetime import timedelta

        from datensee.status import WatchdogConfig, _watchdog_check

        cfg = WatchdogConfig(
            max_runtime=None,
            max_failure_rate=0.5,
            failure_grace_period=timedelta(minutes=1),
            idle_timeout=None,
        )
        state = self._state(started_seconds_ago=600.0)
        info = JobInfo(state=JobState.RUNNING, failures_written=80)
        # No tile_count → skip the rate check entirely.
        assert _watchdog_check(cfg, state, info, tile_count=None) is None

    def test_idle_timeout_breach(self) -> None:
        from datetime import timedelta

        from datensee.status import WatchdogConfig, _watchdog_check

        cfg = WatchdogConfig(
            max_runtime=None,
            max_failure_rate=None,
            failure_grace_period=timedelta(seconds=0),
            idle_timeout=timedelta(minutes=5),
        )
        state = self._state(started_seconds_ago=600.0)
        # state.last_progress_at is also 600s ago — no progress observed.
        info = JobInfo(state=JobState.RUNNING, output_tiles_written=0)
        reason = _watchdog_check(cfg, state, info, tile_count=100)
        assert reason is not None
        assert "idle_timeout" in reason

    def test_idle_timer_resets_on_progress(self) -> None:
        from datetime import timedelta

        from datensee.status import WatchdogConfig, _watchdog_check

        cfg = WatchdogConfig(
            max_runtime=None,
            max_failure_rate=None,
            failure_grace_period=timedelta(seconds=0),
            idle_timeout=timedelta(minutes=5),
        )
        state = self._state(started_seconds_ago=600.0)
        # First tick observes 5 tiles written → resets last_progress_at to now.
        info_progress = JobInfo(state=JobState.RUNNING, output_tiles_written=5)
        assert _watchdog_check(cfg, state, info_progress, tile_count=100) is None
        # Second tick (immediately after) sees same value → not yet idle
        # because the last_progress_at was just refreshed.
        info_same = JobInfo(state=JobState.RUNNING, output_tiles_written=5)
        assert _watchdog_check(cfg, state, info_same, tile_count=100) is None

    def test_terminal_state_does_not_trigger_cancel(self) -> None:
        # The polling loop short-circuits on terminal states before
        # cancellation, but the policy itself should also be a no-op
        # so end-state inspections don't trip the watchdog.
        from datetime import timedelta

        from datensee.status import WatchdogConfig, _watchdog_check

        cfg = WatchdogConfig(
            max_runtime=None,
            max_failure_rate=None,
            failure_grace_period=timedelta(seconds=0),
            idle_timeout=timedelta(minutes=5),
        )
        state = self._state(started_seconds_ago=10_000.0)
        info = JobInfo(state=JobState.DONE, output_tiles_written=0)
        assert _watchdog_check(cfg, state, info, tile_count=100) is None


class TestWatchdogTriggered:
    """End-to-end: the poller should cancel the job and raise."""

    def test_max_runtime_cancels_and_raises(self, httpx_mock: HTTPXMock) -> None:
        from datetime import timedelta

        from datensee.status import WatchdogConfig, WatchdogTriggered

        # First poll returns a long-elapsed running job; the watchdog
        # should fire on that tick. We register *both* responses Beam
        # might issue (pytest-httpx auto-asserts that all registered
        # responses are consumed) — the GET for state and the PUT for
        # cancel.
        httpx_mock.add_response(
            method="GET",
            url=f"{_BASE_URL}?view=JOB_VIEW_ALL",
            json={
                "currentState": "JOB_STATE_RUNNING",
                "createTime": "2020-01-01T00:00:00Z",
            },
        )
        httpx_mock.add_response(
            method="PUT",
            url=_BASE_URL,
            json={"currentState": "JOB_STATE_CANCELLED"},
        )

        # max_runtime=0s + failure_grace=0s + an "elapsed" wall clock
        # well past the cap means the very first tick fires.
        cfg = WatchdogConfig(
            max_runtime=timedelta(seconds=0),
            max_failure_rate=None,
            failure_grace_period=timedelta(seconds=0),
            idle_timeout=None,
        )
        try:
            poll_job(
                "job-123",
                "test-project",
                "us-central1",
                "fake-token",
                poll_interval_seconds=0,
                watchdog=cfg,
                tile_count=100,
            )
        except WatchdogTriggered as exc:
            assert "max_runtime" in exc.reason
            assert exc.job_id == "job-123"
        else:
            raise AssertionError("expected WatchdogTriggered")
