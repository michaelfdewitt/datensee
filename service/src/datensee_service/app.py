"""FastAPI wrapper around `datensee.api.export()` for Cloud Run.

This is the FoundrEE → DatensEE boundary: a single-endpoint HTTP service
that accepts an export request, installs the caller-supplied OAuth access
token as pipeline credentials, and hands off to `datensee.api.export()`.

Wire protocol
-------------
POST /submit
    {
        "expression": "<serialized ee expression (JSON string)>",
        "region": { GeoJSON Polygon or MultiPolygon },
        "project": "<gcp-project-id>",
        "output": "gs://bucket/prefix/",
        "access_token": "<end user's OAuth bearer token>",
        "scale": 30,
        "crs": "EPSG:4326",
        "tile_size": 512,
        "dry_run": true,
        "labels": { "foundree": "1" },
        "region_gcp": "us-central1",
        "temp_location": "gs://bucket/tmp/"
    }

    → 200 { "ok": true,  "job_id": "...", "tile_count": 42,
            "pixel_size": 0.00027, "crs": "EPSG:4326", "scale": 30, ... }
    → 200 { "ok": false, "error": "...", "error_kind": "..." }
    → 400 for malformed JSON or schema-level failures (detail is scrubbed —
      field names/locations only, never field values)

We always return 200 for business-level failures (auth, validation,
datensee runtime errors) so the caller's HTTP client doesn't treat them
as transport errors — the `ok` field is the source of truth.

POST /submit-task (Cloud Tasks target)
    { "task_id": "...", "user_id": "...", "access_token": "..." }

    State machine (Spanner `ExportTasks` row):
        PENDING --claim (read-write txn)--> RUNNING
        RUNNING --submit ok--> SUBMITTED       (200, best-effort write)
        RUNNING --permanent error--> FAILED    (200, best-effort write)
        RUNNING --transient error--> PENDING   (503 → Cloud Tasks retries)
    A delivery that finds the row in any non-PENDING state returns
    200 { "ok": true, "skipped": "already claimed" } — the claim is the
    dedup point, so Cloud Tasks' at-least-once delivery never produces
    duplicate Dataflow jobs.

Security
--------
- Service-to-service auth is enforced by Cloud Run IAM (`roles/run.invoker`
  on the FoundrEE backend SA). The invoker's Google-signed ID token lives
  in the `Authorization: Bearer ...` header and is validated by Cloud Run
  before our handler runs — we never touch it.
- The end user's EE access token travels in the request body. It is
  explicitly stripped from every log line via `_scrub()` before the
  request is logged, and the Credentials object is dropped as soon as
  `datensee.export()` returns. Request-validation errors are likewise
  scrubbed: only field names/locations are echoed, never values.
- FastAPI/uvicorn do not log request bodies by default. We do not enable
  body logging.
"""

from __future__ import annotations

import json
import logging
import os
import re
import traceback
from collections.abc import Callable
from typing import Any, Literal

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

log = logging.getLogger("datensee.service")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

app = FastAPI(title="datensee-service", version="0.1.0")


class SubmitRequest(BaseModel):
    """Export request payload. Matches `datensee.api.export()` kwargs."""

    expression: str
    region: dict[str, Any]
    project: str
    output: str
    access_token: str | None = None
    scale: float = 30.0
    crs: str = "EPSG:4326"
    tile_size: int = Field(default=512, alias="tile_size")
    dry_run: bool = False
    labels: dict[str, str] | None = None
    region_gcp: str = "us-central1"
    temp_location: str | None = None

    model_config = {"populate_by_name": True}


def _scrub(req: SubmitRequest) -> dict[str, Any]:
    """Return a log-safe view of the request — never includes access_token."""
    data = req.model_dump()
    data.pop("access_token", None)
    return data


def _error(kind: str, message: str, **extra: Any) -> dict[str, Any]:
    """Uniform error envelope shared by every endpoint.

    Every error response carries `ok`, `error`, and `error_kind`;
    endpoint-specific fields (e.g. `dry_run`, `detail`) ride along via
    ``extra``.
    """
    return {"ok": False, "error": message, "error_kind": kind, **extra}


def _field(extractor: Callable[[], Any]) -> Any:
    """Evaluate a response-field extractor, degrading to None on any error.

    Used when building success responses *after* a Dataflow job has been
    submitted: an attribute-drift bug in response shaping must degrade a
    field to null, never 500 — a 500 here makes the caller retry an
    already-launched job and duplicate it.
    """
    try:
        return extractor()
    except Exception as exc:
        log.error("response field extraction failed (job already submitted): %s", exc)
        return None


def _scrubbed_validation_detail(
    exc: RequestValidationError,
) -> list[dict[str, str | list[str]]]:
    """Rebuild validation-error detail with field names/locations only.

    FastAPI's default 422 body echoes the offending input value — which,
    for a mistyped `access_token`, reflects the caller's secret back over
    the wire. We keep only `loc` (which field) and `type` (what class of
    error), never `input`, `msg`, `ctx`, or `url`.
    """
    return [
        {
            "loc": [str(part) for part in err.get("loc", ())],
            "type": str(err.get("type", "unknown")),
        }
        for err in exc.errors()
    ]


@app.exception_handler(RequestValidationError)
async def request_validation_error(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Scrubbed handler for malformed request bodies.

    - `/submit-task` is a Cloud Tasks target: any non-2xx is retried, and a
      malformed body is permanent, so it must get 200 with `ok: false`.
    - Other paths get 400 (not FastAPI's default 422 whose detail echoes
      input values).
    """
    detail = _scrubbed_validation_detail(exc)
    fields = ", ".join("/".join(entry["loc"]) for entry in detail) or "<body>"
    message = (
        f"request body failed validation for: {fields}. Field values are "
        "never echoed back — fix the named fields and resend."
    )
    status_code = 200 if request.url.path == "/submit-task" else 400
    return JSONResponse(
        status_code=status_code,
        content=_error("validation", message, detail=detail),
    )


@app.get("/health")
def health() -> JSONResponse:
    """Liveness probe. 503 when datensee is not importable, 200 otherwise."""
    try:
        import datensee

        return JSONResponse(
            {
                "ok": True,
                "datensee_version": getattr(datensee, "__version__", "unknown"),
            }
        )
    except Exception as exc:
        log.error("health check: datensee import failed: %s", exc)
        return JSONResponse(
            status_code=503,
            content=_error(
                "import",
                f"failed to import datensee: {exc}. The container image is "
                "missing or shipped a broken datensee package — redeploy.",
            ),
        )


@app.post("/submit")
def submit(req: SubmitRequest) -> JSONResponse:
    """Submit an Earth Engine export job via datensee."""
    log.info("submit request: %s", _scrub(req))

    if not req.dry_run and not req.access_token:
        return JSONResponse(
            _error(
                "auth",
                "missing access_token. The service refuses to submit real "
                "Dataflow jobs without an end-user OAuth token — ambient "
                "application-default credentials are not trusted for "
                "untrusted workbench code.",
                dry_run=req.dry_run,
            )
        )

    try:
        import datensee
        from datensee import api as datensee_api
    except Exception as exc:
        log.error("datensee import failed: %s", exc)
        return JSONResponse(
            _error("import", f"datensee not installed: {exc}", dry_run=req.dry_run)
        )

    credentials = None
    if req.access_token:
        try:
            from google.oauth2.credentials import Credentials

            credentials = Credentials(token=req.access_token)
        except Exception as exc:
            log.error("failed to build Credentials from access_token: %s", exc)
            return JSONResponse(
                _error(
                    "auth", f"failed to build Credentials: {exc}", dry_run=req.dry_run
                )
            )

    labels: dict[str, str] = (
        {str(k): str(v) for k, v in req.labels.items()}
        if req.labels
        else {"foundree": "1"}
    )

    temp_location = req.temp_location or (
        req.output.rstrip("/") + "/_tmp" if req.output.startswith("gs://") else None
    )

    # Dry runs without credentials rely on datensee.export(dry_run=True)
    # skipping the ensure_auth() ADC bootstrap; real submits always carry
    # explicit credentials.
    try:
        try:
            result = datensee_api.export(
                ee_expression=req.expression,
                region=req.region,
                project=req.project,
                output=req.output,
                scale=req.scale,
                crs=req.crs,
                tile_size=req.tile_size,
                runner="dataflow",
                region_gcp=req.region_gcp,
                temp_location=temp_location,
                labels=labels,
                dry_run=req.dry_run,
                credentials=credentials,
            )
        except ValueError as exc:  # includes pydantic.ValidationError
            return JSONResponse(_error("validation", str(exc), dry_run=req.dry_run))
        except Exception as exc:
            log.error("datensee.export failed: %s\n%s", exc, traceback.format_exc())
            return JSONResponse(
                _error("runtime", f"datensee.export failed: {exc}", dry_run=req.dry_run)
            )
    finally:
        credentials = None  # noqa: F841

    # From here on the Dataflow job may already be running: every field is
    # extracted defensively and serialization failures degrade rather than
    # 500 (a 500 would make the caller retry and launch a duplicate job).
    payload: dict[str, Any] = {
        "ok": True,
        "dry_run": req.dry_run,
        "job_id": _field(lambda: result.job_id),
        "tile_count": _field(lambda: result.config.tile_count),
        "output": _field(lambda: result.config.output.output_path),
        "pixel_size": _field(lambda: result.config.tile_grid.pixel_size),
        "crs": _field(lambda: result.config.tile_grid.crs),
        "scale": req.scale,
        "labels": _field(
            lambda: (
                result.config.runner.dataflow.labels
                if result.config.runner.dataflow is not None
                else None
            )
        ),
        "datensee_version": getattr(datensee, "__version__", "unknown"),
    }
    try:
        return JSONResponse(payload)
    except Exception as exc:
        log.error(
            "submit response serialization failed after submission: %s\n%s",
            exc,
            traceback.format_exc(),
        )
        return JSONResponse(
            {
                "ok": True,
                "dry_run": req.dry_run,
                "job_id": _field(
                    lambda: str(result.job_id) if result.job_id is not None else None
                ),
                "error": "response serialization degraded — see service logs",
            }
        )


# ---------------------------------------------------------------------------
# /submit-task — Cloud Tasks target backed by Spanner state
# ---------------------------------------------------------------------------


class TaskSubmitRequest(BaseModel):
    """Cloud Tasks target payload — minimal, with task_id for Spanner lookup."""

    task_id: str
    user_id: str
    access_token: str


# TODO: typed exception in submit.py — until then, the Flex Template HTTP
# status is only available inside the RuntimeError message text.
_FLEX_LAUNCH_HTTP_STATUS = re.compile(r"Flex Template launch failed \(HTTP (\d{3})\)")


def _classify_export_error(
    exc: Exception,
) -> Literal["validation", "transient", "runtime"]:
    """Classify an `export()` failure for retry semantics.

    - "validation": permanent caller error (bad geometry, bad expression) —
      write FAILED, return 200 so Cloud Tasks stops.
    - "transient": infrastructure blip (network transport errors including
      timeouts/connect failures, Flex Template launch 429/5xx) — reset the
      task to PENDING and return 503 so Cloud Tasks retries.
    - "runtime": anything unrecognized — treated as permanent (FAILED + 200)
      so an unknown permanent error can never retry forever.
    """
    if isinstance(exc, ValueError):  # pydantic.ValidationError subclasses ValueError
        return "validation"
    # TransportError covers httpx.TimeoutException, ConnectError, and the
    # rest of httpx's network-level failures.
    if isinstance(exc, httpx.TransportError):
        return "transient"
    match = _FLEX_LAUNCH_HTTP_STATUS.search(str(exc))
    if match:
        status = int(match.group(1))
        if status == 429 or status >= 500:
            return "transient"
    return "runtime"


def _get_spanner_db():  # noqa: ANN202 — spanner types are import-heavy; lazy on purpose
    """Lazy singleton for the Spanner database handle."""
    if not hasattr(_get_spanner_db, "_db"):
        from google.cloud import spanner

        instance_id = os.environ.get("SPANNER_INSTANCE", "foundree-tasks")
        database_id = os.environ.get("SPANNER_DATABASE", "foundree")
        project = os.environ.get("FOUNDREE_GCP_PROJECT", "foundree-e521c")
        client = spanner.Client(project=project)
        instance = client.instance(instance_id)
        _get_spanner_db._db = instance.database(database_id)
    return _get_spanner_db._db


_CLAIMED = "claimed"
_NOT_FOUND = "not_found"
_ALREADY_CLAIMED = "already_claimed"


def _claim_task(task_id: str, user_id: str) -> tuple[str, dict[str, Any] | None]:
    """Atomically claim a task: PENDING → RUNNING in one read-write txn.

    The transaction reads the row and transitions it to RUNNING before any
    export work happens, so Cloud Tasks' at-least-once delivery cannot
    double-submit: exactly one delivery wins the claim, every other one
    observes a non-PENDING state and skips.

    Returns:
        ("claimed", payload_dict) when this call won the claim,
        ("already_claimed", None) when the row exists but is not PENDING,
        ("not_found", None) when the row does not exist.
    """
    from google.cloud.spanner import COMMIT_TIMESTAMP, KeySet

    db = _get_spanner_db()

    def _txn(transaction: Any) -> tuple[str, dict[str, Any] | None]:
        rows = list(
            transaction.read(
                table="ExportTasks",
                columns=["Payload", "State"],
                keyset=KeySet(keys=[[user_id, task_id]]),
            )
        )
        if not rows:
            return (_NOT_FOUND, None)
        payload_str, state = rows[0]
        if state != "PENDING":
            log.info("Task %s is %s, not PENDING — already claimed", task_id, state)
            return (_ALREADY_CLAIMED, None)
        transaction.update(
            table="ExportTasks",
            columns=["UserId", "TaskId", "State", "UpdatedAt"],
            values=[[user_id, task_id, "RUNNING", COMMIT_TIMESTAMP]],
        )
        return (_CLAIMED, json.loads(payload_str))

    return db.run_in_transaction(_txn)


def _update_task_state(
    task_id: str,
    user_id: str,
    state: str,
    *,
    job_id: str | None = None,
    error: str | None = None,
    error_kind: str | None = None,
) -> None:
    """Update a task's state in Spanner."""
    from google.cloud.spanner import COMMIT_TIMESTAMP

    db = _get_spanner_db()
    columns = ["UserId", "TaskId", "State", "UpdatedAt"]
    values: list[Any] = [user_id, task_id, state, COMMIT_TIMESTAMP]
    if job_id is not None:
        columns.append("DataflowJobId")
        values.append(job_id)
    if error is not None:
        columns.append("Error")
        values.append(error)
    if error_kind is not None:
        columns.append("ErrorKind")
        values.append(error_kind)
    with db.batch() as batch:
        batch.update(
            table="ExportTasks",
            columns=columns,
            values=[values],
        )


def _try_update_task_state(
    task_id: str,
    user_id: str,
    state: str,
    *,
    job_id: str | None = None,
    error: str | None = None,
    error_kind: str | None = None,
) -> None:
    """Best-effort state write — logs loudly on failure, never raises.

    Used for every write after the claim: once the export attempt has run
    (and in particular once a Dataflow job has been submitted), a Spanner
    write failure must not turn into a non-2xx response, or Cloud Tasks
    would redeliver and duplicate the job.
    """
    try:
        _update_task_state(
            task_id, user_id, state, job_id=job_id, error=error, error_kind=error_kind
        )
    except Exception as exc:
        log.error(
            "SPANNER STATE WRITE FAILED for task %s (user %s, target state %s): %s — "
            "the row is stuck in RUNNING; reconcile manually against Dataflow "
            "job %s.\n%s",
            task_id,
            user_id,
            state,
            exc,
            job_id or "<none>",
            traceback.format_exc(),
        )


@app.post("/submit-task")
def submit_task(req: TaskSubmitRequest) -> JSONResponse:
    """Cloud Tasks target — claims the task in Spanner, submits, updates state.

    Returns 200 for permanent business-logic failures (so Cloud Tasks does
    not retry them) and 503 for transient infrastructure failures (task
    reset to PENDING so the retry can re-claim it). See the module
    docstring for the full state machine.
    """
    log.info("submit-task: task_id=%s user_id=%s", req.task_id, req.user_id)

    status, payload = _claim_task(req.task_id, req.user_id)
    if status == _NOT_FOUND:
        log.warning("Task %s not found for user %s", req.task_id, req.user_id)
        return JSONResponse(
            _error(
                "not_found",
                f"task {req.task_id} not found for user {req.user_id} — "
                "nothing to submit. Create the ExportTasks row before "
                "enqueueing the Cloud Task.",
            )
        )
    if status == _ALREADY_CLAIMED:
        return JSONResponse({"ok": True, "skipped": "already claimed"})
    assert payload is not None  # status == _CLAIMED

    try:
        from datensee import api as datensee_api
    except Exception as exc:
        log.error("datensee import failed: %s", exc)
        _try_update_task_state(
            req.task_id, req.user_id, "FAILED", error=str(exc), error_kind="import"
        )
        return JSONResponse(_error("import", f"datensee not installed: {exc}"))

    credentials = None
    try:
        from google.oauth2.credentials import Credentials

        credentials = Credentials(token=req.access_token)
    except Exception as exc:
        log.error("failed to build Credentials: %s", exc)
        _try_update_task_state(
            req.task_id,
            req.user_id,
            "FAILED",
            error=f"failed to build Credentials: {exc}",
            error_kind="auth",
        )
        return JSONResponse(_error("auth", f"auth failed: {exc}"))

    project = payload.get("project", "")
    output = payload.get("output", "")
    dry_run = payload.get("dry_run", False)
    labels = {"foundree": "1"}
    temp_location = payload.get("temp_location") or (
        output.rstrip("/") + "/_tmp" if output.startswith("gs://") else None
    )

    try:
        result = datensee_api.export(
            ee_expression=payload.get("expression", ""),
            region=payload.get("region", {}),
            project=project,
            output=output,
            scale=payload.get("scale", 30.0),
            crs=payload.get("crs", "EPSG:4326"),
            tile_size=payload.get("tile_size", 512),
            runner="dataflow",
            region_gcp=payload.get("region_gcp", "us-central1"),
            temp_location=temp_location,
            labels=labels,
            dry_run=dry_run,
            credentials=credentials,
        )
    except Exception as exc:
        kind = _classify_export_error(exc)
        if kind == "transient":
            log.warning(
                "transient failure for task %s: %s — resetting to PENDING so "
                "Cloud Tasks retries",
                req.task_id,
                exc,
            )
            _try_update_task_state(req.task_id, req.user_id, "PENDING")
            return JSONResponse(
                status_code=503,
                content=_error(
                    "transient",
                    f"transient infrastructure failure: {exc}. Task reset to "
                    "PENDING; Cloud Tasks will retry.",
                ),
            )
        log.error(
            "datensee.export failed for task %s (%s): %s\n%s",
            req.task_id,
            kind,
            exc,
            traceback.format_exc(),
        )
        _try_update_task_state(
            req.task_id, req.user_id, "FAILED", error=str(exc), error_kind=kind
        )
        return JSONResponse(_error(kind, str(exc)))
    finally:
        credentials = None  # noqa: F841

    # The Dataflow job (if any) is submitted: from here on, nothing may
    # produce a non-2xx response — a retry would duplicate the job.
    job_id = _field(lambda: result.job_id)
    _try_update_task_state(req.task_id, req.user_id, "SUBMITTED", job_id=job_id)
    log.info("Task %s submitted: job_id=%s (dry_run=%s)", req.task_id, job_id, dry_run)

    response: dict[str, Any] = {
        "ok": True,
        "task_id": req.task_id,
        "job_id": job_id,
        "tile_count": _field(lambda: result.config.tile_count),
    }
    try:
        return JSONResponse(response)
    except Exception as exc:
        log.error("submit-task response serialization failed after submission: %s", exc)
        return JSONResponse({"ok": True, "task_id": req.task_id})


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception) -> JSONResponse:
    """Last-resort handler — never leak internals back to the caller."""
    log.error("unhandled exception: %s\n%s", exc, traceback.format_exc())
    return JSONResponse(
        status_code=500,
        content=_error("runtime", "internal server error"),
    )
