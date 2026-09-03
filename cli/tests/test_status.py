"""Tests for datensee.status: Dataflow job status polling.

Uses pytest-httpx to mock the Dataflow REST API. Credentials are stubbed
with an in-memory subclass of google.auth.credentials.Credentials whose
refresh() mints deterministic tokens without touching the network (the
google-auth refresh transport is `requests`-based, so pytest-httpx would
not intercept a real refresh anyway).
"""

from __future__ import annotations

from datetime import timedelta

import google.auth.credentials
import pytest
from pytest_httpx import HTTPXMock
from rich.table import Table

from datensee.status import (
    TERMINAL_STATES,
    JobInfo,
    JobState,
    WatchdogConfig,
    WatchdogTriggered,
    _cancel_job,
    _fetch_job_info,
    _parse_elapsed,
    _parse_metrics,
    _render_status_table,
    _watchdog_check,
    _WatchdogState,
    poll_job,
)

_BASE_URL = (
    "https://dataflow.googleapis.com/v1b3/projects/test-project/locations/us-central1/jobs/job-123"
)


class FakeCredentials(google.auth.credentials.Credentials):
    """In-memory Credentials: refresh() mints a deterministic new token.

    Inherits the real ``.valid`` / ``.expired`` properties (a ``None``
    token makes the credential invalid, forcing a proactive refresh).
    """

    def __init__(self, token: str | None = "fake-token") -> None:
        super().__init__()
        self.token = token
        self.refresh_count = 0

    def refresh(self, request: object) -> None:  # noqa: ARG002  # google-auth interface
        self.refresh_count += 1
        self.token = f"refreshed-token-{self.refresh_count}"


class TestJobState:
    def test_terminal_states(self) -> None:
        assert TERMINAL_STATES == {
            JobState.DONE,
            JobState.FAILED,
            JobState.CANCELLED,
            JobState.DRAINED,
            JobState.UPDATED,
        }

    @pytest.mark.parametrize(
        "state",
        [
            JobState.PENDING,
            JobState.QUEUED,
            JobState.RUNNING,
            JobState.DRAINING,
            JobState.CANCELLING,
            JobState.STOPPED,
            JobState.UNKNOWN,
        ],
    )
    def test_non_terminal_states(self, state: JobState) -> None:
        assert state not in TERMINAL_STATES

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("JOB_STATE_PENDING", JobState.PENDING),
            ("JOB_STATE_QUEUED", JobState.QUEUED),
            ("JOB_STATE_RUNNING", JobState.RUNNING),
            ("JOB_STATE_DRAINING", JobState.DRAINING),
            ("JOB_STATE_CANCELLING", JobState.CANCELLING),
            ("JOB_STATE_STOPPED", JobState.STOPPED),
            ("JOB_STATE_DONE", JobState.DONE),
            ("JOB_STATE_FAILED", JobState.FAILED),
            ("JOB_STATE_CANCELLED", JobState.CANCELLED),
            ("JOB_STATE_DRAINED", JobState.DRAINED),
            ("JOB_STATE_UPDATED", JobState.UPDATED),
            ("JOB_STATE_UNKNOWN", JobState.UNKNOWN),
        ],
    )
    def test_all_dataflow_state_strings_map(self, raw: str, expected: JobState) -> None:
        assert JobState(raw) is expected


class TestFetchJobInfo:
    def test_running_job(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            url=f"{_BASE_URL}?view=JOB_VIEW_ALL",
            json={
                "currentState": "JOB_STATE_RUNNING",
                "createTime": "2026-03-17T10:00:00Z",
            },
        )
        info = _fetch_job_info(_BASE_URL, FakeCredentials())
        assert info.state == JobState.RUNNING
        assert info.elapsed_seconds is not None
        assert info.elapsed_seconds > 0

    def test_done_job(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            url=f"{_BASE_URL}?view=JOB_VIEW_ALL",
            json={"currentState": "JOB_STATE_DONE"},
        )
        info = _fetch_job_info(_BASE_URL, FakeCredentials())
        assert info.state == JobState.DONE

    def test_http_error_returns_unknown(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            url=f"{_BASE_URL}?view=JOB_VIEW_ALL",
            status_code=500,
        )
        info = _fetch_job_info(_BASE_URL, FakeCredentials())
        assert info.state == JobState.UNKNOWN

    def test_unknown_state_string(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            url=f"{_BASE_URL}?view=JOB_VIEW_ALL",
            json={"currentState": "JOB_STATE_BRAND_NEW"},
        )
        info = _fetch_job_info(_BASE_URL, FakeCredentials())
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
                                "name": "ElementCount",
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
        info = _fetch_job_info(_BASE_URL, FakeCredentials())
        assert info.elements_produced == 42
        assert info.elements_total == 100
        assert info.current_workers == 5


class TestCredentialRefresh:
    """Fix 2: token staleness; the poller holds a Credentials object and
    refreshes it proactively (``.valid``) and reactively (on 401)."""

    def test_valid_token_sent_as_bearer_header(self, httpx_mock: HTTPXMock) -> None:
        creds = FakeCredentials(token="fresh-token")
        httpx_mock.add_response(
            url=f"{_BASE_URL}?view=JOB_VIEW_ALL",
            match_headers={"Authorization": "Bearer fresh-token"},
            json={"currentState": "JOB_STATE_RUNNING"},
        )
        info = _fetch_job_info(_BASE_URL, creds)
        assert info.state == JobState.RUNNING
        assert creds.refresh_count == 0

    def test_invalid_credentials_refreshed_before_request(self, httpx_mock: HTTPXMock) -> None:
        creds = FakeCredentials(token=None)  # .valid is False
        httpx_mock.add_response(
            url=f"{_BASE_URL}?view=JOB_VIEW_ALL",
            match_headers={"Authorization": "Bearer refreshed-token-1"},
            json={"currentState": "JOB_STATE_DONE"},
        )
        info = _fetch_job_info(_BASE_URL, creds)
        assert info.state == JobState.DONE
        assert creds.refresh_count == 1

    def test_401_refreshes_and_retries_once(self, httpx_mock: HTTPXMock) -> None:
        """A token revoked server-side before local expiry: first response
        is 401, the poller must refresh and retry with the new token."""
        creds = FakeCredentials(token="stale-but-locally-valid")
        httpx_mock.add_response(
            url=f"{_BASE_URL}?view=JOB_VIEW_ALL",
            match_headers={"Authorization": "Bearer stale-but-locally-valid"},
            status_code=401,
        )
        httpx_mock.add_response(
            url=f"{_BASE_URL}?view=JOB_VIEW_ALL",
            match_headers={"Authorization": "Bearer refreshed-token-1"},
            json={"currentState": "JOB_STATE_RUNNING"},
        )
        info = _fetch_job_info(_BASE_URL, creds)
        assert info.state == JobState.RUNNING
        assert creds.refresh_count == 1

    def test_persistent_401_degrades_to_unknown(self, httpx_mock: HTTPXMock) -> None:
        """Only one retry: a second 401 degrades the tick to UNKNOWN
        instead of looping."""
        creds = FakeCredentials()
        httpx_mock.add_response(url=f"{_BASE_URL}?view=JOB_VIEW_ALL", status_code=401)
        httpx_mock.add_response(url=f"{_BASE_URL}?view=JOB_VIEW_ALL", status_code=401)
        info = _fetch_job_info(_BASE_URL, creds)
        assert info.state == JobState.UNKNOWN
        assert creds.refresh_count == 1

    def test_cancel_refreshes_invalid_credentials(self, httpx_mock: HTTPXMock) -> None:
        """The cancel path must use a fresh token, not the poll loop's
        possibly-hours-old one."""
        creds = FakeCredentials(token=None)
        httpx_mock.add_response(
            method="PUT",
            url=_BASE_URL,
            match_headers={"Authorization": "Bearer refreshed-token-1"},
            json={"currentState": "JOB_STATE_CANCELLED"},
        )
        assert _cancel_job("job-123", "test-project", "us-central1", creds) is True
        assert creds.refresh_count == 1

    def test_cancel_failure_returns_false(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="PUT", url=_BASE_URL, status_code=500)
        assert _cancel_job("job-123", "test-project", "us-central1", FakeCredentials()) is False


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

    @pytest.mark.parametrize("state", list(JobState))
    def test_every_state_renders(self, state: JobState) -> None:
        table = _render_status_table("job-789", JobInfo(state=state))
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
            FakeCredentials(),
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
            FakeCredentials(),
            poll_interval_seconds=0,
        )
        assert state == JobState.DONE

    def test_poll_failed_job(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            url=f"{_BASE_URL}?view=JOB_VIEW_ALL",
            json={"currentState": "JOB_STATE_FAILED"},
        )
        # A FAILED terminal state triggers one messages lookup for the reason.
        httpx_mock.add_response(
            url=f"{_BASE_URL}/messages?minimumImportance=JOB_MESSAGE_ERROR&pageSize=100",
            json={
                "jobMessages": [
                    {"messageImportance": "JOB_MESSAGE_ERROR", "messageText": "Workflow failed."}
                ]
            },
        )
        state = poll_job(
            "job-123",
            "test-project",
            "us-central1",
            FakeCredentials(),
            poll_interval_seconds=0,
        )
        assert state == JobState.FAILED

    def test_poll_drained_job_terminates(self, httpx_mock: HTTPXMock) -> None:
        """Fix 3: DRAINED is terminal; a drained job must not poll forever."""
        httpx_mock.add_response(
            url=f"{_BASE_URL}?view=JOB_VIEW_ALL",
            json={"currentState": "JOB_STATE_DRAINED"},
        )
        state = poll_job(
            "job-123",
            "test-project",
            "us-central1",
            FakeCredentials(),
            poll_interval_seconds=0,
        )
        assert state == JobState.DRAINED

    def test_poll_draining_then_drained(self, httpx_mock: HTTPXMock) -> None:
        """DRAINING is non-terminal; the loop keeps polling through it."""
        httpx_mock.add_response(
            url=f"{_BASE_URL}?view=JOB_VIEW_ALL",
            json={"currentState": "JOB_STATE_DRAINING"},
        )
        httpx_mock.add_response(
            url=f"{_BASE_URL}?view=JOB_VIEW_ALL",
            json={"currentState": "JOB_STATE_DRAINED"},
        )
        state = poll_job(
            "job-123",
            "test-project",
            "us-central1",
            FakeCredentials(),
            poll_interval_seconds=0,
        )
        assert state == JobState.DRAINED


class TestWatchdog:
    """The watchdog policies are pure-state functions; we exercise
    ``_watchdog_check`` directly so the tests don't have to spin up a
    fake polling loop."""

    def _state(self, started_seconds_ago: float = 0.0) -> _WatchdogState:
        s = _WatchdogState.fresh()
        # Adjust started_at backwards by N seconds so the check thinks
        # the poll session has been attached for a while.
        s.started_at -= started_seconds_ago
        s.last_progress_at -= started_seconds_ago
        return s

    def test_max_runtime_breach(self) -> None:
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
        cfg = WatchdogConfig(
            max_runtime=timedelta(hours=12),
            max_failure_rate=None,
            idle_timeout=None,
        )
        state = self._state(started_seconds_ago=120.0)
        info = JobInfo(state=JobState.RUNNING)
        assert _watchdog_check(cfg, state, info, tile_count=10) is None

    def test_max_runtime_disabled_when_none(self) -> None:
        cfg = WatchdogConfig(max_runtime=None, max_failure_rate=None, idle_timeout=None)
        state = self._state(started_seconds_ago=10_000_000.0)
        info = JobInfo(state=JobState.RUNNING)
        assert _watchdog_check(cfg, state, info, tile_count=10) is None

    def test_max_runtime_uses_job_age_not_poll_session(self) -> None:
        """Fix 4: reattaching to an 11-hour-old job must not grant another
        12 h. The poll session just started, but createTime says 13 h."""
        cfg = WatchdogConfig(
            max_runtime=timedelta(hours=12),
            max_failure_rate=None,
            idle_timeout=None,
        )
        state = self._state()  # fresh poll session
        info = JobInfo(state=JobState.RUNNING, elapsed_seconds=13 * 3600.0)
        reason = _watchdog_check(cfg, state, info, tile_count=10)
        assert reason is not None
        assert "max_runtime" in reason
        # The message reports job age, not the ~0 min poll session.
        assert f"{13 * 60:.1f} min" in reason

    def test_max_runtime_young_job_old_poll_session_passes(self) -> None:
        """Converse of the reattach case: a long-lived poll session against
        a young job (job restarted, session reused) must not fire."""
        cfg = WatchdogConfig(
            max_runtime=timedelta(hours=12),
            max_failure_rate=None,
            idle_timeout=None,
        )
        state = self._state(started_seconds_ago=13 * 3600.0)
        info = JobInfo(state=JobState.RUNNING, elapsed_seconds=3600.0)  # 1 h old
        assert _watchdog_check(cfg, state, info, tile_count=10) is None

    def test_max_runtime_falls_back_to_session_elapsed(self) -> None:
        """When createTime is unavailable (degraded UNKNOWN ticks), the
        poll-session clock still provides a backstop."""
        cfg = WatchdogConfig(
            max_runtime=timedelta(minutes=5),
            max_failure_rate=None,
            idle_timeout=None,
        )
        state = self._state(started_seconds_ago=400.0)
        info = JobInfo(state=JobState.UNKNOWN, elapsed_seconds=None)
        reason = _watchdog_check(cfg, state, info, tile_count=10)
        assert reason is not None
        assert "max_runtime" in reason

    def test_failure_rate_breach_after_grace(self) -> None:
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

    def test_failure_rate_grace_anchored_to_job_age(self) -> None:
        """Fix 4: the grace period is relative to job creation, not poll
        attach. Reattaching to a 20-minute-old failure storm fires
        immediately despite the poll session being seconds old."""
        cfg = WatchdogConfig(
            max_runtime=None,
            max_failure_rate=0.5,
            failure_grace_period=timedelta(minutes=10),
            idle_timeout=None,
        )
        state = self._state()  # fresh poll session
        info = JobInfo(state=JobState.RUNNING, elapsed_seconds=1200.0, failures_written=80)
        reason = _watchdog_check(cfg, state, info, tile_count=100)
        assert reason is not None
        assert "max_failure_rate" in reason

    def test_failure_rate_skipped_without_tile_count(self) -> None:
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
        cfg = WatchdogConfig(
            max_runtime=None,
            max_failure_rate=None,
            failure_grace_period=timedelta(seconds=0),
            idle_timeout=timedelta(minutes=5),
        )
        state = self._state(started_seconds_ago=600.0)
        # state.last_progress_at is also 600s ago: no progress observed.
        info = JobInfo(state=JobState.RUNNING, output_tiles_written=0)
        reason = _watchdog_check(cfg, state, info, tile_count=100)
        assert reason is not None
        assert "idle_timeout" in reason

    def test_idle_timer_resets_on_progress(self) -> None:
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

    def test_idle_small_counter_progress_beside_large_pinned_counter(self) -> None:
        """Fix 1: after the fetch phase, elements_produced pins at
        ~tile_count while output_tiles_written creeps up in small numbers
        during two-tier assembly. Any counter increasing must reset the stall
        timer: the old max()-based check let the pinned large counter
        mask the small one's progress and cancelled healthy jobs."""
        cfg = WatchdogConfig(
            max_runtime=None,
            max_failure_rate=None,
            failure_grace_period=timedelta(seconds=0),
            idle_timeout=timedelta(minutes=5),
        )
        state = self._state()

        # Tick 1: fetch phase done (1000 elements), assembly starting.
        tick1 = JobInfo(state=JobState.RUNNING, elements_produced=1000, output_tiles_written=1)
        assert _watchdog_check(cfg, state, tick1, tile_count=1000) is None

        # 10 idle-minutes pass; assembly writes ONE more output tile.
        state.last_progress_at -= 600.0
        tick2 = JobInfo(state=JobState.RUNNING, elements_produced=1000, output_tiles_written=2)
        assert _watchdog_check(cfg, state, tick2, tile_count=1000) is None

        # Another 10 idle-minutes with NO counter movement → genuinely
        # stalled, and only now does the watchdog fire.
        state.last_progress_at -= 600.0
        tick3 = JobInfo(state=JobState.RUNNING, elements_produced=1000, output_tiles_written=2)
        reason = _watchdog_check(cfg, state, tick3, tile_count=1000)
        assert reason is not None
        assert "idle_timeout" in reason

    def test_idle_each_counter_tracked_independently(self) -> None:
        """A failures_written increase alone (dead-lettering is progress
        toward termination) also resets the timer, even when every other
        counter is pinned."""
        cfg = WatchdogConfig(
            max_runtime=None,
            max_failure_rate=None,
            failure_grace_period=timedelta(seconds=0),
            idle_timeout=timedelta(minutes=5),
        )
        state = self._state()
        tick1 = JobInfo(state=JobState.RUNNING, elements_produced=500, failures_written=3)
        assert _watchdog_check(cfg, state, tick1, tile_count=None) is None
        state.last_progress_at -= 600.0
        tick2 = JobInfo(state=JobState.RUNNING, elements_produced=500, failures_written=4)
        assert _watchdog_check(cfg, state, tick2, tile_count=None) is None

    def test_terminal_state_does_not_trigger_cancel(self) -> None:
        # The polling loop short-circuits on terminal states before
        # cancellation, but the policy itself should also be a no-op
        # so end-state inspections don't trip the watchdog.
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
        # First poll returns a long-elapsed running job; the watchdog
        # should fire on that tick. We register *both* responses Beam
        # might issue (pytest-httpx auto-asserts that all registered
        # responses are consumed): the GET for state and the PUT for
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

        # max_runtime=0s + failure_grace=0s + a createTime years in the
        # past means the very first tick fires.
        cfg = WatchdogConfig(
            max_runtime=timedelta(seconds=0),
            max_failure_rate=None,
            failure_grace_period=timedelta(seconds=0),
            idle_timeout=None,
        )
        with pytest.raises(WatchdogTriggered) as exc_info:
            poll_job(
                "job-123",
                "test-project",
                "us-central1",
                FakeCredentials(),
                poll_interval_seconds=0,
                watchdog=cfg,
                tile_count=100,
            )
        assert "max_runtime" in exc_info.value.reason
        assert exc_info.value.job_id == "job-123"
        # Cancel succeeded → no failure disclaimer in the message.
        assert "cancel request FAILED" not in exc_info.value.reason

    def test_failed_cancel_is_reported_not_claimed(self, httpx_mock: HTTPXMock) -> None:
        """Fix 2: when the cancel request fails, WatchdogTriggered must say
        so instead of implying the job was cancelled."""
        httpx_mock.add_response(
            method="GET",
            url=f"{_BASE_URL}?view=JOB_VIEW_ALL",
            json={
                "currentState": "JOB_STATE_RUNNING",
                "createTime": "2020-01-01T00:00:00Z",
            },
        )
        httpx_mock.add_response(method="PUT", url=_BASE_URL, status_code=500)

        cfg = WatchdogConfig(
            max_runtime=timedelta(seconds=0),
            max_failure_rate=None,
            failure_grace_period=timedelta(seconds=0),
            idle_timeout=None,
        )
        with pytest.raises(WatchdogTriggered) as exc_info:
            poll_job(
                "job-123",
                "test-project",
                "us-central1",
                FakeCredentials(),
                poll_interval_seconds=0,
                watchdog=cfg,
                tile_count=100,
            )
        assert "cancel request FAILED" in exc_info.value.reason
        assert "gcloud dataflow jobs cancel job-123" in exc_info.value.reason


class TestFailureSummary:
    """``failure_summary`` distils job messages into actionable lines."""

    _MESSAGES_URL = (
        "https://dataflow.googleapis.com/v1b3/projects/p/locations/r/jobs/j/messages"
        "?minimumImportance=JOB_MESSAGE_ERROR&pageSize=100"
    )
    _JOB_URL = (
        "https://dataflow.googleapis.com/v1b3/projects/p/locations/r/jobs/j?view=JOB_VIEW_ALL"
    )

    @staticmethod
    def _error(text: str) -> dict[str, str]:
        return {"messageImportance": "JOB_MESSAGE_ERROR", "messageText": text}

    def test_collapses_repeats_and_keeps_the_most_detailed(self, httpx_mock: HTTPXMock) -> None:
        from datensee.status import failure_summary

        stockout = (
            "Startup of the worker pool in r failed to bring up any of the desired 4 workers. "
            "ZONE_RESOURCE_POOL_EXHAUSTED: Instance 'harness-{}' creation failed."
        )
        httpx_mock.add_response(
            url=self._MESSAGES_URL,
            json={
                "jobMessages": [
                    self._error(stockout.format("a")),
                    self._error("\n" + stockout.format("b")),
                    self._error("Workflow failed."),
                    self._error("Workflow failed. Causes: S01:FetchTiles failed."),
                ]
            },
        )
        lines = failure_summary("j", "p", "r", FakeCredentials())
        assert lines == [
            "Dataflow: " + stockout.format("b"),
            "Dataflow: Workflow failed. Causes: S01:FetchTiles failed.",
        ]

    def test_follows_pagination_to_reach_the_terminal_error(self, httpx_mock: HTTPXMock) -> None:
        from datensee.status import failure_summary

        httpx_mock.add_response(
            url=self._MESSAGES_URL,
            json={"jobMessages": [self._error("Early error.")], "nextPageToken": "t2"},
        )
        httpx_mock.add_response(
            url=self._MESSAGES_URL + "&pageToken=t2",
            json={"jobMessages": [self._error("Workflow failed.")]},
        )
        assert failure_summary("j", "p", "r", FakeCredentials()) == [
            "Dataflow: Early error.",
            "Dataflow: Workflow failed.",
        ]

    def test_launcher_failure_points_at_console_log(self, httpx_mock: HTTPXMock) -> None:
        from datensee.status import failure_summary

        httpx_mock.add_response(
            url=self._MESSAGES_URL,
            json={
                "jobMessages": [
                    self._error("Error occurred in the launcher container: Template launch failed.")
                ]
            },
        )
        httpx_mock.add_response(
            url=self._JOB_URL,
            json={
                "environment": {
                    "sdkPipelineOptions": {"options": {"stagingLocation": "gs://b/tmp/staging"}}
                }
            },
        )
        lines = failure_summary("j", "p", "r", FakeCredentials())
        assert lines[-1] == (
            "Launcher stack trace (not in Cloud Logging): "
            "gcloud storage cat gs://b/tmp/staging/template_launches/j/console_logs"
        )

    def test_unreachable_messages_endpoint_is_silent(self, httpx_mock: HTTPXMock) -> None:
        from datensee.status import failure_summary

        httpx_mock.add_response(url=self._MESSAGES_URL, status_code=503)
        assert failure_summary("j", "p", "r", FakeCredentials()) == []

    def test_poll_job_attaches_reasons_to_job_info(self, httpx_mock: HTTPXMock) -> None:
        """Callback consumers get the reason structurally, not as console output."""
        httpx_mock.add_response(
            url=f"{_BASE_URL}?view=JOB_VIEW_ALL", json={"currentState": "JOB_STATE_FAILED"}
        )
        httpx_mock.add_response(
            url=f"{_BASE_URL}/messages?minimumImportance=JOB_MESSAGE_ERROR&pageSize=100",
            json={"jobMessages": [self._error("Workflow failed.")]},
        )
        seen: list[JobInfo] = []
        state = poll_job(
            "job-123",
            "test-project",
            "us-central1",
            FakeCredentials(),
            poll_interval_seconds=0,
            status_callback=seen.append,
        )
        assert state == JobState.FAILED
        assert seen[-1].failure_reasons == ["Dataflow: Workflow failed."]
