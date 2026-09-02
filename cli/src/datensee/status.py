"""Job status polling, watchdog cost controls, and log streaming.

Polls the Dataflow REST API to surface job state, elapsed time, and
tile-level progress metrics. Dataflow metrics lag ~30-60s behind reality.

Auth is credential-object based: the poller holds a
:class:`google.auth.credentials.Credentials` and refreshes it whenever it
goes stale (proactively via ``credentials.valid``, reactively on a 401),
so multi-hour polls survive the ~1 h bearer-token lifetime.

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
from typing import Any

import google.auth.exceptions
import google.auth.transport.requests
import httpx
from google.auth.credentials import Credentials
from rich.console import Console
from rich.live import Live
from rich.table import Table

console = Console()

_DATAFLOW_API = (
    "https://dataflow.googleapis.com/v1b3/projects/{project}/locations/{region}/jobs/{job_id}"
)


class JobState(StrEnum):
    # Non-terminal states.
    PENDING = "JOB_STATE_PENDING"
    QUEUED = "JOB_STATE_QUEUED"
    RUNNING = "JOB_STATE_RUNNING"
    DRAINING = "JOB_STATE_DRAINING"
    CANCELLING = "JOB_STATE_CANCELLING"
    STOPPED = "JOB_STATE_STOPPED"
    UNKNOWN = "JOB_STATE_UNKNOWN"
    # Terminal states.
    DONE = "JOB_STATE_DONE"
    FAILED = "JOB_STATE_FAILED"
    CANCELLED = "JOB_STATE_CANCELLED"
    DRAINED = "JOB_STATE_DRAINED"
    UPDATED = "JOB_STATE_UPDATED"


TERMINAL_STATES = frozenset(
    {JobState.DONE, JobState.FAILED, JobState.CANCELLED, JobState.DRAINED, JobState.UPDATED}
)


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
    # Why a FAILED job failed, from :func:`failure_summary`. Filled on the
    # terminal tick only, so callback consumers (notebooks, services) get
    # the reason structurally instead of as console output.
    failure_reasons: list[str] = field(default_factory=list)


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
    """Hard wall-clock cap on total job runtime, measured from the job's
    Dataflow ``createTime`` (so reattaching to an old job does not grant
    it a fresh budget). The poller cancels the Dataflow job and raises
    :class:`WatchdogTriggered` if exceeded. Catches every kind of stall
    (rate-limit loop, hung worker, autoscaler spiral) without needing
    each failure mode handled individually. Default 12 h. Set ``None``
    to disable."""

    max_failure_rate: float | None = field(default=0.5)
    """Cancel if the ratio of dead-lettered tiles to total tiles exceeds
    this fraction (in ``[0, 1]``), once :attr:`failure_grace_period` has
    elapsed. Catches the "every tile is 429ing" pattern early instead of
    grinding through hours of doomed retries. Default 0.5. Set ``None``
    to disable (e.g. when the caller intentionally tolerates high
    failure rates)."""

    failure_grace_period: timedelta = field(default=timedelta(minutes=10))
    """Skip the failure-rate check for this long after job creation.
    Gives the autoscaler + worker pool time to ramp up before we judge
    whether a high failure rate is structural vs. transient. Default
    10 min."""

    idle_timeout: timedelta | None = field(default=timedelta(minutes=20))
    """Cancel if no progress is observed on *any* progress counter for
    this long. Catches stalls that aren't outright failures — e.g. a
    thread stuck in an unbounded retry loop while everything else has
    finished. Default 20 min. Set ``None`` to disable."""


class WatchdogTriggered(RuntimeError):
    """Raised when a watchdog policy cancels a running Dataflow job."""

    def __init__(self, reason: str, job_id: str, info: JobInfo) -> None:
        super().__init__(reason)
        self.reason = reason
        self.job_id = job_id
        self.info = info


# JobInfo counters watched by the idle detector. Each is tracked
# independently: an increase in ANY of them resets the stall timer.
# A single max() over these is wrong — once the fetch phase completes,
# elements_produced pins at ~tile_count and dominates the max, so a
# slow-but-healthy two-tier assembly phase (output_tiles_written creeping up
# in small numbers) would never register as progress.
_PROGRESS_COUNTERS: tuple[str, ...] = (
    "output_tiles_written",
    "tiles_written",
    "compute_tiles_assembled",
    "elements_produced",
    "failures_written",
)


@dataclass
class _WatchdogState:
    """Mutable poll-loop state used by the watchdog policies."""

    started_at: float
    last_progress_at: float
    last_counter_values: dict[str, int] = field(default_factory=dict)

    @classmethod
    def fresh(cls) -> _WatchdogState:
        now = time.monotonic()
        return cls(started_at=now, last_progress_at=now)


def _job_age_seconds(state: _WatchdogState, info: JobInfo) -> float:
    """Job age in seconds, anchored to the job's Dataflow createTime.

    ``info.elapsed_seconds`` is parsed from the API's ``createTime``, so
    it reflects how long the *job* has existed — not how long this poll
    session has been attached. Falls back to poll-session elapsed time
    when the API response carried no createTime (e.g. degraded UNKNOWN
    ticks), so the runtime cap still eventually fires even with a dead
    API path.
    """
    if info.elapsed_seconds is not None:
        return info.elapsed_seconds
    return time.monotonic() - state.started_at


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
    job_age = _job_age_seconds(state, info)

    # 1. Hard wall-clock cap on job age (createTime-anchored, so
    # reattaching to an 11-hour-old job does not grant another 12 h).
    if config.max_runtime is not None and job_age > config.max_runtime.total_seconds():
        return (
            f"max_runtime exceeded: job has been running for {job_age / 60:.1f} min, "
            f"cap is {config.max_runtime.total_seconds() / 60:.1f} min"
        )

    # 2. Failure-rate circuit breaker (after grace period, also
    # job-age-anchored — a reattach must not restart the grace clock).
    if (
        config.max_failure_rate is not None
        and tile_count
        and tile_count > 0
        and info.failures_written is not None
        and job_age > config.failure_grace_period.total_seconds()
    ):
        rate = info.failures_written / tile_count
        if rate > config.max_failure_rate:
            return (
                f"max_failure_rate exceeded: {info.failures_written}/{tile_count} tiles "
                f"({rate:.0%}) failed after {job_age / 60:.1f} min, "
                f"threshold is {config.max_failure_rate:.0%}"
            )

    # 3. Idle-progress detector. Each counter in _PROGRESS_COUNTERS is
    # tracked against its own last-seen value; ANY counter increasing
    # resets the stall timer. The completed-output counters
    # (output_tiles_written / tiles_written) only move once the two-tier
    # GroupByKey has fired (i.e. after the entire fetch phase finishes),
    # while ElementCount on the fetch step's output PCollection moves per
    # successful fetch and failures_written moves per dead-letter —
    # between them, real forward progress is always reflected somewhere.
    if config.idle_timeout is not None:
        progressed = False
        for counter in _PROGRESS_COUNTERS:
            value: int | None = getattr(info, counter)
            if value is None:
                continue
            if value > state.last_counter_values.get(counter, 0):
                state.last_counter_values[counter] = value
                progressed = True
        now = time.monotonic()
        if progressed:
            state.last_progress_at = now
        elif (
            info.state == JobState.RUNNING
            and now - state.last_progress_at > config.idle_timeout.total_seconds()
            and job_age > config.failure_grace_period.total_seconds()
        ):
            stalled_min = (now - state.last_progress_at) / 60
            return (
                f"idle_timeout exceeded: no progress for {stalled_min:.1f} min "
                f"(threshold {config.idle_timeout.total_seconds() / 60:.0f} min); "
                f"last counter values {state.last_counter_values or '{}'}"
            )

    return None


# ---------------------------------------------------------------------------
# Authenticated transport
# ---------------------------------------------------------------------------


def _refresh(credentials: Credentials) -> None:
    """Refresh the credential's access token via the google-auth transport."""
    credentials.refresh(google.auth.transport.requests.Request())


def _bearer_header(credentials: Credentials) -> dict[str, str]:
    """Authorization header from the credential, refreshing it if stale."""
    if not credentials.valid:
        _refresh(credentials)
    return {"Authorization": f"Bearer {credentials.token}"}


def _authorized_request(
    method: str,
    url: str,
    credentials: Credentials,
    *,
    params: dict[str, str] | None = None,
    json: dict[str, object] | None = None,
) -> httpx.Response:
    """Issue an authenticated request, keeping the token fresh.

    Refreshes proactively when the credential reports itself invalid or
    expired, and reactively (refresh + retry exactly once) when the
    server answers 401 — covering tokens revoked server-side before
    their local expiry. Raises for any non-2xx final response.
    """
    with httpx.Client(timeout=30) as client:
        response = client.request(
            method, url, headers=_bearer_header(credentials), params=params, json=json
        )
        if response.status_code == 401:
            _refresh(credentials)
            response = client.request(
                method, url, headers=_bearer_header(credentials), params=params, json=json
            )
        response.raise_for_status()
        return response


def _cancel_job(job_id: str, project: str, region: str, credentials: Credentials) -> bool:
    """Best-effort Dataflow job cancel with a freshly-refreshed token.

    Returns True if the cancel request was accepted, False otherwise.
    Transport and auth errors are logged, not raised — the watchdog
    raises regardless, and its message must reflect whether the cancel
    actually landed."""
    url = _DATAFLOW_API.format(project=project, region=region, job_id=job_id)
    try:
        _authorized_request("PUT", url, credentials, json={"requestedState": "JOB_STATE_CANCELLED"})
        return True
    except (httpx.HTTPError, google.auth.exceptions.GoogleAuthError) as exc:
        console.print(
            f"[yellow]Warning: failed to cancel Dataflow job {job_id}: {exc}. "
            f"Cancel manually with: gcloud dataflow jobs cancel {job_id} "
            f"--region={region}[/yellow]"
        )
        return False


def poll_job(
    job_id: str,
    project: str,
    region: str,
    credentials: Credentials,
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
        credentials: Google credentials for the Dataflow API. Refreshed
            automatically whenever the access token goes stale, so polls
            longer than a token lifetime (~1 h) keep working.
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
        WatchdogTriggered: If a watchdog policy cancels the job (or tries
            to — when the cancel request itself fails, the exception
            message says so instead of claiming the job was cancelled).
    """
    url = _DATAFLOW_API.format(project=project, region=region, job_id=job_id)

    config = watchdog if watchdog is not None else WatchdogConfig()
    state = _WatchdogState.fresh()

    def tick() -> JobInfo:
        info = _fetch_job_info(url, credentials)
        if info.state == JobState.FAILED:
            info.failure_reasons = failure_summary(job_id, project, region, credentials)
        reason = _watchdog_check(config, state, info, tile_count)
        if reason is not None and info.state not in TERMINAL_STATES:
            console.print(f"\n[red]Watchdog cancelling job {job_id}:[/red] {reason}")
            if not _cancel_job(job_id, project, region, credentials):
                reason += (
                    "; cancel request FAILED — the job may still be running. "
                    f"Cancel manually with: gcloud dataflow jobs cancel {job_id} "
                    f"--region={region}"
                )
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


_LAUNCHER_FAILURE_MARKER = "launcher container"
"""Unversioned Dataflow prose; the only in-band signal that a Flex Template
launch (not a worker) failed. JobMessage carries no structured cause."""


def failure_summary(
    job_id: str,
    project: str,
    region: str,
    credentials: Credentials,
    *,
    max_errors: int = 3,
) -> list[str]:
    """Explain a failed Dataflow job from its job messages.

    Dataflow repeats some errors every 30 s with a fresh instance name
    (worker-pool stockouts), and emits both a bare ``Workflow failed.``
    and a ``Workflow failed. Causes: …`` — so messages are collapsed on
    their first sentence, keeping the most detailed text per group.

    Args:
        job_id: Dataflow job ID.
        project: GCP project the job ran in.
        region: Dataflow region.
        credentials: Credentials for the Dataflow API.
        max_errors: Cap on distinct error lines returned (newest kept).

    Returns:
        ``"Dataflow: <message>"`` lines, newest last, plus — when the job
        died inside the Flex Template launcher, whose stack trace never
        reaches Cloud Logging — the GCS path of the launcher console log.
        Empty when the API is unreachable; the caller already knows the
        state.
    """
    base = _DATAFLOW_API.format(project=project, region=region, job_id=job_id)
    try:
        messages = _all_job_messages(base, credentials, minimum_importance="JOB_MESSAGE_ERROR")
    except (httpx.HTTPError, google.auth.exceptions.GoogleAuthError, ValueError):
        return []

    grouped: dict[str, str] = {}
    for message in messages:
        text = str(message.get("messageText", "")).strip()
        if not text:
            continue
        key = text.split(". ", 1)[0].rstrip(".")
        if len(text) >= len(grouped.get(key, "")):
            grouped[key] = text
    errors = list(grouped.values())
    lines = [f"Dataflow: {e}" for e in errors[-max_errors:]]

    if any(_LAUNCHER_FAILURE_MARKER in e for e in errors):
        staging = _staging_location(base, credentials)
        if staging:
            lines.append(
                "Launcher stack trace (not in Cloud Logging): gcloud storage cat "
                f"{staging.rstrip('/')}/template_launches/{job_id}/console_logs"
            )
    return lines


def _all_job_messages(
    base_url: str, credentials: Credentials, *, minimum_importance: str
) -> list[dict[str, Any]]:
    """Page through ``jobs.messages.list`` (ascending time) and return every message."""
    messages: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        params = {"minimumImportance": minimum_importance, "pageSize": "100"}
        if page_token:
            params["pageToken"] = page_token
        body = _authorized_request("GET", base_url + "/messages", credentials, params=params).json()
        messages.extend(body.get("jobMessages", []))
        page_token = body.get("nextPageToken")
        if not page_token:
            return messages


def _staging_location(base_url: str, credentials: Credentials) -> str | None:
    """The job's ``stagingLocation`` pipeline option — where the launcher writes its log."""
    try:
        job = _authorized_request(
            "GET", base_url, credentials, params={"view": "JOB_VIEW_ALL"}
        ).json()
    except (httpx.HTTPError, google.auth.exceptions.GoogleAuthError, ValueError):
        return None
    options = ((job.get("environment") or {}).get("sdkPipelineOptions") or {}).get("options") or {}
    value = options.get("stagingLocation")
    return str(value) if value else None


def _fetch_job_info(url: str, credentials: Credentials) -> JobInfo:
    """Fetch job state and metrics from the Dataflow REST API.

    Uses ?view=JOB_VIEW_ALL to include metrics (element counts, workers).
    Metrics lag ~30-60s behind reality. Degrades to UNKNOWN on transport,
    auth, or parse errors — a single bad tick must not kill the poll loop.
    """
    try:
        response = _authorized_request("GET", url, credentials, params={"view": "JOB_VIEW_ALL"})
        data = response.json()
    except (httpx.HTTPError, google.auth.exceptions.GoogleAuthError, KeyError, ValueError):
        return JobInfo(state=JobState.UNKNOWN)

    raw_state = data.get("currentState", "JOB_STATE_UNKNOWN")
    try:
        state = JobState(raw_state)
    except ValueError:
        state = JobState.UNKNOWN

    # Elapsed time from job createTime.
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

        # Beam-system metrics for the progress display + watchdog idle
        # signal. Runner v2 emits per-PCollection counts as `ElementCount`
        # (not `elements_produced` — that name was the pre-Runner-v2
        # spelling). We track the maximum across all output_user_name
        # contexts as a coarse "is the pipeline doing anything?" signal,
        # which lets the watchdog detect two-tier fetch-phase progress before
        # GroupByKey lets the per-output-tile counters move.
        if name == "ElementCount" and ctx.get("output_user_name"):
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
        JobState.QUEUED: "yellow",
        JobState.RUNNING: "cyan",
        JobState.DRAINING: "magenta",
        JobState.CANCELLING: "magenta",
        JobState.STOPPED: "yellow",
        JobState.DONE: "green",
        JobState.FAILED: "red",
        JobState.CANCELLED: "magenta",
        JobState.DRAINED: "magenta",
        JobState.UPDATED: "green",
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

    for line in info.failure_reasons:
        table.add_row("Reason", f"[red]{line}[/red]")
    return table
