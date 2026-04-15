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
        "max_qps": 100,
        "dry_run": true,
        "labels": { "foundree": "1" },
        "region_gcp": "us-central1",
        "temp_location": "gs://bucket/tmp/"
    }

    → 200 { "ok": true,  "job_id": "...", "tile_count": 42, ... }
    → 200 { "ok": false, "error": "...", "error_kind": "..." }
    → 400 for malformed JSON or schema-level failures

We always return 200 for business-level failures (auth, validation,
datensee runtime errors) so the caller's HTTP client doesn't treat them
as transport errors — the `ok` field is the source of truth.

Security
--------
- Service-to-service auth is enforced by Cloud Run IAM (`roles/run.invoker`
  on the FoundrEE backend SA). The invoker's Google-signed ID token lives
  in the `Authorization: Bearer ...` header and is validated by Cloud Run
  before our handler runs — we never touch it.
- The end user's EE access token travels in the request body. It is
  explicitly stripped from every log line via `_scrub()` before the
  request is logged, and the Credentials object is dropped as soon as
  `datensee.export()` returns.
- FastAPI/uvicorn do not log request bodies by default. We do not enable
  body logging.
"""

from __future__ import annotations

import logging
import os
import traceback
from typing import Any

from fastapi import FastAPI, Request
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
    max_qps: int = Field(default=100, alias="max_qps")
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


def _error(kind: str, message: str, dry_run: bool = False) -> dict[str, Any]:
    return {
        "ok": False,
        "dry_run": dry_run,
        "job_id": None,
        "tile_count": None,
        "output": None,
        "datensee_version": None,
        "error": message,
        "error_kind": kind,
    }


@app.get("/health")
def health() -> dict[str, Any]:
    """Liveness probe. Also reports whether datensee is importable."""
    try:
        import datensee

        return {"ok": True, "datensee_version": getattr(datensee, "__version__", "unknown")}
    except Exception as exc:
        return {"ok": False, "error": f"failed to import datensee: {exc}"}


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
            )
        )

    try:
        import datensee
        from datensee import api as datensee_api
        from datensee import notebook as datensee_notebook
    except Exception as exc:
        log.error("datensee import failed: %s", exc)
        return JSONResponse(_error("import", f"datensee not installed: {exc}", dry_run=req.dry_run))

    credentials = None
    if req.access_token:
        try:
            from google.oauth2.credentials import Credentials

            credentials = Credentials(token=req.access_token)
        except Exception as exc:
            log.error("failed to build Credentials from access_token: %s", exc)
            return JSONResponse(
                _error("auth", f"failed to build Credentials: {exc}", dry_run=req.dry_run)
            )

    # Dry-run without credentials still needs to skip ensure_auth (which
    # would otherwise require ambient ADC on the container). Real submits
    # have `credentials` set, so datensee.export() uses those directly.
    if req.dry_run and credentials is None:
        datensee_notebook.ensure_auth = lambda: None  # type: ignore[assignment]

    labels: dict[str, str] = (
        {str(k): str(v) for k, v in req.labels.items()}
        if req.labels
        else {"foundree": "1"}
    )

    temp_location = req.temp_location or (
        req.output.rstrip("/") + "/_tmp" if req.output.startswith("gs://") else None
    )

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
                max_qps=req.max_qps,
                labels=labels,
                dry_run=req.dry_run,
                credentials=credentials,
            )
        except ValueError as exc:
            return JSONResponse(_error("validation", str(exc), dry_run=req.dry_run))
        except Exception as exc:
            log.error("datensee.export failed: %s\n%s", exc, traceback.format_exc())
            return JSONResponse(
                _error("runtime", f"datensee.export failed: {exc}", dry_run=req.dry_run)
            )
    finally:
        credentials = None  # noqa: F841

    applied_labels = (
        result.config.runner.dataflow.labels
        if result.config.runner.dataflow is not None
        else None
    )
    return JSONResponse(
        {
            "ok": True,
            "dry_run": req.dry_run,
            "job_id": result.job_id,
            "tile_count": result.config.tile_count,
            "output": result.config.output.output_path,
            "scale": result.config.tile_grid.scale_meters,
            "crs": result.config.tile_grid.crs,
            "labels": applied_labels,
            "datensee_version": getattr(datensee, "__version__", "unknown"),
        }
    )


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception) -> JSONResponse:
    """Last-resort handler — never leak internals back to the caller."""
    log.error("unhandled exception: %s\n%s", exc, traceback.format_exc())
    return JSONResponse(
        status_code=500,
        content=_error("runtime", "internal server error"),
    )
