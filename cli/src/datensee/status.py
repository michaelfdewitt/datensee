"""Job status polling, watchdog cost controls, and log streaming.

Polls the Dataflow REST API to surface job state, elapsed time, and
tile-level progress metrics. Dataflow metrics lag ~30-60s behind reality.

The poll loop also enforces a configurable :class:`WatchdogConfig` —
hard wall-clock cap, failure-rate circuit breaker, and idle-progress
detector. Any breach cancels the Dataflow job and raises
:class:`WatchdogTriggered`. Cost-control defaults are conservative so an
unattended export can't run away (real incident: a 48-hour stuck job
that burned ~167 vCPU-hours mostly idle in retry-sleep). Each control
is independently configurable, and ``None`` disables a given check.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum

import httpx
from rich.console import Console
from rich.live import Live
from rich.table import Table

console = Console()

_DATAFLOW_API = (
    "https://dataflow.googleapis.com/v1b3/projects/{project}/locations/{region}/jobs/{job_id}"
)


class JobState(StrEnum):
    PENDING = "JOB_STATE_PENDING"
    RUNNING = "JOB_STATE_RUNNING"
    DONE = "JOB_STATE_DONE"
    FAILED = "JOB_STATE_FAILED"
    CANCELLED = "JOB_STATE_CANCELLED"
    UNKNOWN = "JOB_STATE_UNKNOWN"


TERMINAL_STATES = {JobState.DONE, JobState.FAILED, JobState.CANCELLED}


@dataclass
class JobInfo:
    """Parsed job state and metrics from the Dataflow API."""

    state: JobState
    elapsed_seconds: float | None = None
    elements_produced: int | None = None
    elements_total: int | None = None
    current_workers: int | None = None
    # User counters from the datensee namespace (Beam Metrics). Populated
    # by `_parse_metrics`; absent until the first DoFn increment lands.
    failures_written: int | None = None
    output_tiles_written: int | None = None
    tiles_written: int | None = None
    compute_tiles_assembled: int | None = None


# ---------------------------------------------------------------------------
# Watchdog: cost-control circuit breakers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WatchdogConfig:
    """Cost-control policy applied by :func:`poll_job` each tick.

    Each field is independently configurable; setting one to ``None``
    disables that specific check. The defaults are deliberately
    conservative — an unattended pipeline can't run away.
    """

    max_runtime: timedelta | None = field(default=timedelta(hours=12))
    """Hard wall-clock cap on total job runtime. The poller cancels the
    Dataflow job and raises :class:`WatchdogTriggered` if exceeded.
    Catches every kind of stall (rate-limit loop, hung worker, autoscaler
    spiral) without needing each failure mode handled individually.
    Default 12 h. Set ``None`` to disable."""

    max_failure_rate: float | None = field(default=0.5)
    """Cancel if the ratio of dead-lettered tiles to total tiles exceeds
    this fraction (in ``[0, 1]``), once :attr:`failure_grace_period` has
    elapsed. Catches the "every tile is 429ing" pattern early instead of
    grinding through hours of doomed retries. Default 0.5. Set ``None``
    to disable (e.g. when the caller intentionally tolerates high
    failure rates)."""

    failure_grace_period: timedelta = field(default=timedelta(minutes=10))
    """Skip the failure-rate check for this long after job start. Gives
    the autoscaler + worker pool time to ramp up before we judge whether
    a high failure rate is structural vs. transient. Default 10 min."""

    idle_timeout: timedelta | None = field(default=timedelta(minutes=20))
    """Cancel if no progress is observed (no increase in
    ``output_tiles_written`` *or* ``tiles_written``) for this long.
    Catches stalls that aren't outright failures — e.g. a thread stuck
    in an unbounded retry loop while everything else has finished.
    Default 20 min. Set ``None`` to disable."""


class WatchdogTriggered(RuntimeError):
    """Raised when a watchdog policy cancels a running Dataflow job."""

    def __init__(self, reason: str, job_id: str, info: JobInfo) -> None:
        super().__init__(reason)
        self.reason = reason
        self.job_id = job_id
        self.info = info


@dataclass
class _WatchdogState:
    """Mutable poll-loop state used by the watchdog policies."""

    started_at: float
    last_progress_at: float
    last_progress_value: int

    @classmethod
    def fresh(cls) -> _WatchdogState:
        now = time.monotonic()
        return cls(started_at=now, last_progress_at=now, last_progress_value=0)


def _watchdog_check(
    config: WatchdogConfig,
    state: _WatchdogState,
    info: JobInfo,
    tile_count: int | None,
) -> str | None:
    """Apply each enabled policy. Returns a cancellation reason or None.

    Order matters: max_runtime > max_failure_rate > idle_timeout.
    The runtime cap is the single hardest backstop; we surface it first
    so its message is the one the user sees in catastrophic stalls.
    """
    elapsed = time.monotonic() - state.started_at

    # 1. Hard wall-clock cap.
    if config.max_runtime is not None and elapsed > config.max_runtime.total_seconds():
        return (
            f"max_runtime exceeded: job ran for {elapsed / 60:.1f} min, "
            f"cap is {config.max_runtime.total_seconds() / 60:.1f} min"
        )

    # 2. Failure-rate circuit breaker (after grace period).
    if (
        config.max_failure_rate is not None
        and tile_count
        and tile_count > 0
        and info.failures_written is not None
        and elapsed > config.failure_grace_period.total_seconds()
    ):
        rate = info.failures_written / tile_count
        if rate > config.max_failure_rate:
            return (
                f"max_failure_rate exceeded: {info.failures_written}/{tile_count} tiles "
                f"({rate:.0%}) failed after {elapsed / 60:.1f} min, "
                f"threshold is {config.max_failure_rate:.0%}"
            )

    # 3. Idle-progress detector. Reset the stall timer whenever the
    # max of the two completion counters increases.
    if config.idle_timeout is not None:
        progress = max(
            info.output_tiles_written or 0,
            info.tiles_written or 0,
        )
        now = time.monotonic()
        if progress > state.last_progress_value:
            state.last_progress_value = progress
            state.last_progress_at = now
        elif (
            info.state == JobState.RUNNING
            and now - state.last_progress_at > config.idle_timeout.total_seconds()
            and elapsed > config.failure_grace_period.total_seconds()
        ):
            stalled_min = (now - state.last_progress_at) / 60
            return (
                f"idle_timeout exceeded: no progress for {stalled_min:.1f} min "
                f"(threshold {config.idle_timeout.total_seconds() / 60:.0f} min); "
                f"last counter value {state.last_progress_value}"
            )

    return None


def _cancel_job(job_id: str, project: str, region: str, access_token: str) -> None:
    """Best-effort Dataflow job cancel. Logs and swallows transport errors —
    the watchdog raises regardless, and the alternative (uncaught
    exception in a poll loop after we've already detected runaway) is
    worse than a stuck job we tried to cancel."""
    url = _DATAFLOW_API.format(project=project, region=region, job_id=job_id)
    try:
        with httpx.Client(timeout=30) as client:
            response = client.put(
                url,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json={"requestedState": "JOB_STATE_CANCELLED"},
            )
            response.raise_for_status()
    except httpx.HTTPError as exc:
        console.print(
            f"[yellow]Warning: failed to cancel Dataflow job {job_id}: {exc}. "
            f"Cancel manually with: gcloud dataflow jobs cancel {job_id}[/yellow]"
        )


def poll_job(
    job_id: str,
    project: str,
    region: str,
    access_token: str,
    *,
    poll_interval_seconds: int = 15,
    status_callback: Callable[[JobInfo], None] | None = None,
    watchdog: WatchdogConfig | None = None,
    tile_count: int | None = None,
) -> JobState:
    """Poll a Dataflow job until it reaches a terminal state.

    Args:
        job_id: Dataflow job ID.
        project: GCP project ID.
        region: Dataflow region (e.g. 'us-central1').
        access_token: OAuth2 bearer token for the Dataflow API.
        poll_interval_seconds: How often to poll.
        status_callback: Optional callback(JobInfo) called on each poll tick.
            When provided, Rich Live display is suppressed.
        watchdog: Cost-control policy. Default :class:`WatchdogConfig` with
            12 h runtime cap, 50% failure-rate threshold, 20 min idle
            timeout. Pass ``WatchdogConfig(max_runtime=None, ...)`` to
            disable individual checks.
        tile_count: Total compute tile count (used by the failure-rate
            check). When ``None`` the failure-rate check is skipped.

    Returns:
        Final JobState.

    Raises:
        WatchdogTriggered: If a watchdog policy cancels the job.
    """
    url = _DATAFLOW_API.format(project=project, region=region, job_id=job_id)
    headers = {"Authorization": f"Bearer {access_token}"}

    config = watchdog if watchdog is not None else WatchdogConfig()
    state = _WatchdogState.fresh()

    def tick() -> JobInfo:
        info = _fetch_job_info(url, headers)
        reason = _watchdog_check(config, state, info, tile_count)
        if reason is not None and info.state not in TERMINAL_STATES:
            console.print(
                f"\n[red]Watchdog cancelling job {job_id}:[/red] {reason}"
            )
            _cancel_job(job_id, project, region, access_token)
            raise WatchdogTriggered(reason, job_id, info)
        return info

    if status_callback is not None:
        while True:
            info = tick()
            status_callback(info)
            if info.state in TERMINAL_STATES:
                break
            time.sleep(poll_interval_seconds)
    else:
        with Live(console=console, refresh_per_second=4) as live:
            while True:
                info = tick()
                live.update(_render_status_table(job_id, info))
                if info.state in TERMINAL_STATES:
                    break
                time.sleep(poll_interval_seconds)

    return info.state


def _fetch_job_info(url: str, headers: dict[str, str]) -> JobInfo:
    """Fetch job state and metrics from the Dataflow REST API.

    Uses ?view=JOB_VIEW_ALL to include metrics (element counts, workers).
    Metrics lag ~30-60s behind reality.
    """
    try:
        with httpx.Client(timeout=30) as client:
            response = client.get(url, headers=headers, params={"view": "JOB_VIEW_ALL"})
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, KeyError, ValueError):
        return JobInfo(state=JobState.UNKNOWN)

    raw_state = data.get("currentState", "JOB_STATE_UNKNOWN")
    try:
        state = JobState(raw_state)
    except ValueError:
        state = JobState.UNKNOWN

    # Elapsed time from currentStateTime (or createTime as fallback)
    elapsed = _parse_elapsed(data)

    # Parse metrics for element counts + datensee user counters.
    metrics = _parse_metrics(data)

    return JobInfo(
        state=state,
        elapsed_seconds=elapsed,
        elements_produced=metrics.elements_produced,
        elements_total=metrics.elements_total,
        current_workers=metrics.current_workers,
        failures_written=metrics.failures_written,
        output_tiles_written=metrics.output_tiles_written,
        tiles_written=metrics.tiles_written,
        compute_tiles_assembled=metrics.compute_tiles_assembled,
    )


def _parse_elapsed(data: dict) -> float | None:
    """Parse elapsed seconds from job create time."""
    create_time = data.get("createTime")
    if not create_time:
        return None
    try:
        created = datetime.fromisoformat(create_time.replace("Z", "+00:00"))
        return (datetime.now(UTC) - created).total_seconds()
    except (ValueError, TypeError):
        return None


@dataclass
class _ParsedMetrics:
    elements_produced: int | None = None
    elements_total: int | None = None
    current_workers: int | None = None
    failures_written: int | None = None
    output_tiles_written: int | None = None
    tiles_written: int | None = None
    compute_tiles_assembled: int | None = None


def _parse_metrics(data: dict) -> _ParsedMetrics:
    """Extract Beam-system + datensee user counters from a Dataflow metrics blob.

    The Dataflow metrics API returns a flat list; each entry's
    ``name.context`` carries the namespace + step. We split the
    Beam-system metrics (used for the live progress display) from the
    datensee user counters (used by the watchdog to detect failure
    storms and stalls).
    """
    metrics_list = (data.get("jobMetrics") or {}).get("metrics", [])
    out = _ParsedMetrics()
    if not metrics_list:
        return out

    for metric in metrics_list:
        name_obj = metric.get("name", {})
        name = name_obj.get("name", "")
        ctx = name_obj.get("context", {}) or {}
        scalar = metric.get("scalar")
        if scalar is None:
            continue
        try:
            value = int(scalar)
        except (ValueError, TypeError):
            continue

        # User-namespace counters (defined by datensee DoFns / writers).
        if ctx.get("namespace") == "datensee":
            if name == "failures_written":
                out.failures_written = value
            elif name == "output_tiles_written":
                out.output_tiles_written = value
            elif name == "tiles_written":
                out.tiles_written = value
            elif name == "compute_tiles_assembled":
                out.compute_tiles_assembled = value
            continue

        # Beam-system metrics for the progress display.
        if name == "elements_produced" and ctx.get("output_user_name"):
            if out.elements_produced is None or value > out.elements_produced:
                out.elements_produced = value
        if name == "elements_added":
            if out.elements_total is None or value > out.elements_total:
                out.elements_total = value
        if name == "current_num_workers":
            out.current_workers = value

    return out


def _render_status_table(job_id: str, info: JobInfo) -> Table:
    """Render a Rich Table showing current job state and metrics."""
    color_map: dict[JobState, str] = {
        JobState.PENDING: "yellow",
        JobState.RUNNING: "cyan",
        JobState.DONE: "green",
        JobState.FAILED: "red",
        JobState.CANCELLED: "magenta",
        JobState.UNKNOWN: "dim",
    }
    color = color_map.get(info.state, "white")

    table = Table(show_header=False, show_edge=False, box=None, pad_edge=False)
    table.add_column("label", style="bold", min_width=12)
    table.add_column("value")

    table.add_row("Job", job_id)
    table.add_row("State", f"[{color}]{info.state.value}[/{color}]")

    if info.elapsed_seconds is not None:
        elapsed_min = info.elapsed_seconds / 60
        table.add_row("Elapsed", f"{elapsed_min:.1f} min")

    if info.current_workers is not None:
        table.add_row("Workers", str(info.current_workers))

    if info.elements_produced is not None:
        total_str = f"/ {info.elements_total}" if info.elements_total else ""
        table.add_row("Tiles", f"{info.elements_produced} {total_str}  [dim](~30s lag)[/dim]")

    # Surface datensee user counters when present — the watchdog reads
    # the same values so they're useful to show in the live display.
    if info.output_tiles_written is not None:
        table.add_row("Output COGs", str(info.output_tiles_written))
    if info.failures_written is not None and info.failures_written > 0:
        table.add_row("Failures", f"[yellow]{info.failures_written}[/yellow]")

    return table
