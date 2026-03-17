"""Job status polling and log streaming.

Polls the Dataflow REST API to surface job state and streams
Cloud Logging output for running jobs.
"""

from __future__ import annotations

import time
from enum import StrEnum

import httpx
from rich.console import Console
from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text

console = Console()

_DATAFLOW_API = "https://dataflow.googleapis.com/v1b3/projects/{project}/locations/{region}/jobs/{job_id}"


class JobState(StrEnum):
    PENDING = "JOB_STATE_PENDING"
    RUNNING = "JOB_STATE_RUNNING"
    DONE = "JOB_STATE_DONE"
    FAILED = "JOB_STATE_FAILED"
    CANCELLED = "JOB_STATE_CANCELLED"
    UNKNOWN = "JOB_STATE_UNKNOWN"


TERMINAL_STATES = {JobState.DONE, JobState.FAILED, JobState.CANCELLED}


def poll_job(
    job_id: str,
    project: str,
    region: str,
    access_token: str,
    *,
    poll_interval_seconds: int = 15,
) -> JobState:
    """Poll a Dataflow job until it reaches a terminal state.

    Args:
        job_id: Dataflow job ID.
        project: GCP project ID.
        region: Dataflow region (e.g. 'us-central1').
        access_token: OAuth2 bearer token for the Dataflow API.
        poll_interval_seconds: How often to poll.

    Returns:
        Final JobState.
    """
    url = _DATAFLOW_API.format(project=project, region=region, job_id=job_id)
    headers = {"Authorization": f"Bearer {access_token}"}

    with Live(console=console, refresh_per_second=4) as live:
        while True:
            state = _fetch_state(url, headers)
            live.update(_render_status(job_id, state))

            if state in TERMINAL_STATES:
                break

            time.sleep(poll_interval_seconds)

    return state


def _fetch_state(url: str, headers: dict[str, str]) -> JobState:
    """Fetch current job state from the Dataflow REST API."""
    try:
        with httpx.Client(timeout=30) as client:
            response = client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
            raw = data.get("currentState", "JOB_STATE_UNKNOWN")
            return JobState(raw)
    except (httpx.HTTPError, KeyError, ValueError):
        return JobState.UNKNOWN


def _render_status(job_id: str, state: JobState) -> Text:
    """Render a Rich Text widget showing current job state."""
    color_map: dict[JobState, str] = {
        JobState.PENDING: "yellow",
        JobState.RUNNING: "cyan",
        JobState.DONE: "green",
        JobState.FAILED: "red",
        JobState.CANCELLED: "magenta",
        JobState.UNKNOWN: "dim",
    }
    color = color_map.get(state, "white")
    return Text(f"Job {job_id}  state={state.value}", style=color)
