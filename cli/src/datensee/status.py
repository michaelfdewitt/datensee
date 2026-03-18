"""Job status polling and log streaming.

Polls the Dataflow REST API to surface job state, elapsed time, and
tile-level progress metrics. Dataflow metrics lag ~30-60s behind reality.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
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


def poll_job(
    job_id: str,
    project: str,
    region: str,
    access_token: str,
    *,
    poll_interval_seconds: int = 15,
    status_callback: Callable[[JobInfo], None] | None = None,
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

    Returns:
        Final JobState.
    """
    url = _DATAFLOW_API.format(project=project, region=region, job_id=job_id)
    headers = {"Authorization": f"Bearer {access_token}"}

    if status_callback is not None:
        while True:
            info = _fetch_job_info(url, headers)
            status_callback(info)
            if info.state in TERMINAL_STATES:
                break
            time.sleep(poll_interval_seconds)
    else:
        with Live(console=console, refresh_per_second=4) as live:
            while True:
                info = _fetch_job_info(url, headers)
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

    # Parse metrics for element counts
    elements_produced, elements_total, workers = _parse_metrics(data)

    return JobInfo(
        state=state,
        elapsed_seconds=elapsed,
        elements_produced=elements_produced,
        elements_total=elements_total,
        current_workers=workers,
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


def _parse_metrics(data: dict) -> tuple[int | None, int | None, int | None]:
    """Extract element counts and worker count from Dataflow job metrics.

    Returns:
        (elements_produced, elements_total, current_workers) — any may be None.
    """
    metrics_list = (data.get("jobMetrics") or {}).get("metrics", [])
    if not metrics_list:
        return None, None, None

    elements_produced: int | None = None
    elements_total: int | None = None
    current_workers: int | None = None

    for metric in metrics_list:
        name_obj = metric.get("name", {})
        name = name_obj.get("name", "")
        scalar = metric.get("scalar")

        if scalar is None:
            continue

        try:
            value = int(scalar)
        except (ValueError, TypeError):
            continue

        if name == "elements_produced" and name_obj.get("context", {}).get("output_user_name"):
            # Sum of all step outputs — take the max as a rough progress indicator
            if elements_produced is None or value > elements_produced:
                elements_produced = value

        if name == "elements_added":
            # Input elements = total tiles
            if elements_total is None or value > elements_total:
                elements_total = value

        if name == "current_num_workers":
            current_workers = value

    return elements_produced, elements_total, current_workers


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

    return table
