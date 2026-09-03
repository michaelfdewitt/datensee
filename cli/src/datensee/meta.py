"""Export metadata sidecar.

Persists export configuration to ``{output}/_export_meta.json`` (CRS, scale,
tile sizes, project, EE expression, and parent :class:`PixelGrid`).

``api.retry()`` validates incoming arguments against this sidecar to ensure
retry child tiles align with the output grid of existing COGs. With the
sidecar present, ``datensee retry`` derives shape arguments and parent grid
offsets automatically.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, model_validator

from datensee.config import PixelGrid

logger = logging.getLogger(__name__)

# Snapshot times above this are nanoseconds, written before the units
# fix. Microseconds for any date through ~year 5138 stay below this.
_NANOS_THRESHOLD: int = 10**17

if TYPE_CHECKING:
    from google.auth.credentials import Credentials
    from google.cloud import storage

EXPORT_META_FILENAME: str = "_export_meta.json"


class ExportMeta(BaseModel):
    """Persisted metadata describing the original export.

    Captures configuration parameters required for retry rounds, including
    the full Earth Engine expression and parent :class:`PixelGrid`. The
    expression is preserved pre-clip as originally provided.

    ``pixel_grid`` stores the parent grid from the original export's
    ``decompose_region`` invocation. Retry requires this grid to reconstruct
    CRS coordinates from local pixel offsets in failures journal records.
    """

    schema_version: int = 1
    crs: str
    scale_meters: float
    tile_size_pixels: int
    output_tile_size_pixels: int | None
    gee_project: str
    ee_expression: str
    snapshot_time: int | None = None
    pixel_grid: PixelGrid | None = None
    nodata: float | None = None
    # Output raster shape as declared at export time. Informational for the
    # pipeline (EE's responses are self-describing), but required by
    # `validate` and inherited by retry rounds. Legacy meta lacks them.
    band_count: int | None = None
    data_type: str | None = None

    @model_validator(mode="after")
    def _migrate_nanos_snapshot_time(self) -> ExportMeta:
        """Convert legacy nanosecond ``snapshot_time`` to microseconds.

        Early metadata files stored Unix nanoseconds. Migrated to
        microseconds at read time so retries against older exports succeed.
        """
        if self.snapshot_time is not None and self.snapshot_time > _NANOS_THRESHOLD:
            migrated = self.snapshot_time // 1_000
            logger.warning(
                "ExportMeta.snapshot_time=%d is in nanoseconds (legacy "
                "format). Migrating in-memory to microseconds=%d. Re-run "
                "the export to persist the corrected value.",
                self.snapshot_time,
                migrated,
            )
            self.snapshot_time = migrated
        return self

    @property
    def ee_expression_sha256(self) -> str:
        """SHA-256 of the persisted expression, computed on demand."""
        return _hash_expression(self.ee_expression)


class ExportMetaMismatch(ValueError):
    """Retry args don't match the original export's persisted shape."""


def _hash_expression(ee_expression: str) -> str:
    return hashlib.sha256(ee_expression.encode("utf-8")).hexdigest()


def build_meta(
    *,
    crs: str,
    scale_meters: float,
    tile_size_pixels: int,
    output_tile_size_pixels: int | None,
    gee_project: str,
    ee_expression: str,
    snapshot_time: int | None = None,
    pixel_grid: PixelGrid | None = None,
    nodata: float | None = None,
    band_count: int | None = None,
    data_type: str | None = None,
) -> ExportMeta:
    return ExportMeta(
        crs=crs,
        scale_meters=scale_meters,
        tile_size_pixels=tile_size_pixels,
        output_tile_size_pixels=output_tile_size_pixels,
        gee_project=gee_project,
        ee_expression=ee_expression,
        snapshot_time=snapshot_time,
        pixel_grid=pixel_grid,
        nodata=nodata,
        band_count=band_count,
        data_type=data_type,
    )


def _gcs_blob(output_path: str, credentials: Credentials | None) -> storage.Blob:
    """Resolve a GCS blob handle for the meta sidecar at ``output_path``."""
    from datensee.auth import gcs_client, split_gcs_uri

    client = gcs_client(credentials)
    bucket_name, prefix = split_gcs_uri(output_path)
    prefix = prefix.rstrip("/")
    blob_name = f"{prefix}/{EXPORT_META_FILENAME}" if prefix else EXPORT_META_FILENAME
    return client.bucket(bucket_name).blob(blob_name)


def write_meta(
    output_path: str,
    meta: ExportMeta,
    *,
    credentials: Credentials | None = None,
) -> None:
    """Write ``_export_meta.json`` to ``output_path``.

    Local paths land on disk; ``gs://`` URIs upload via the GCS client,
    using ``credentials`` if supplied. Existing sidecars are overwritten —
    a fresh export legitimately replaces the prior shape; retry never
    calls this function.
    """
    payload = meta.model_dump_json(indent=2)
    if output_path.startswith("gs://"):
        blob = _gcs_blob(output_path, credentials)
        blob.upload_from_string(payload, content_type="application/json")
        return
    out_dir = Path(output_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / EXPORT_META_FILENAME).write_text(payload, encoding="utf-8")


def read_meta(
    output_path: str,
    *,
    credentials: Credentials | None = None,
) -> ExportMeta | None:
    """Read ``_export_meta.json`` from ``output_path``; return None if absent."""
    if output_path.startswith("gs://"):
        blob = _gcs_blob(output_path, credentials)
        if not blob.exists():
            return None
        return ExportMeta.model_validate_json(blob.download_as_text())
    path = Path(output_path) / EXPORT_META_FILENAME
    if not path.exists():
        return None
    return ExportMeta.model_validate_json(path.read_text(encoding="utf-8"))


def verify_retry_compatibility(
    meta: ExportMeta,
    *,
    crs: str,
    scale_meters: float,
    tile_size_pixels: int,
    output_tile_size_pixels: int | None,
    gee_project: str,
    ee_expression: str,
    nodata: float | None = None,
    band_count: int | None = None,
    data_type: str | None = None,
) -> None:
    """Raise :class:`ExportMetaMismatch` if any retry arg disagrees with ``meta``."""
    mismatches: list[str] = []
    if meta.crs != crs:
        mismatches.append(f"crs: original={meta.crs!r}, retry={crs!r}")
    if meta.scale_meters != scale_meters:
        mismatches.append(f"scale_meters: original={meta.scale_meters}, retry={scale_meters}")
    if meta.tile_size_pixels != tile_size_pixels:
        mismatches.append(
            f"tile_size_pixels: original={meta.tile_size_pixels}, retry={tile_size_pixels}"
        )
    if meta.output_tile_size_pixels != output_tile_size_pixels:
        mismatches.append(
            f"output_tile_size_pixels: original={meta.output_tile_size_pixels}, "
            f"retry={output_tile_size_pixels}"
        )
    if meta.gee_project != gee_project:
        mismatches.append(f"gee_project: original={meta.gee_project!r}, retry={gee_project!r}")
    if meta.nodata != nodata:
        mismatches.append(f"nodata: original={meta.nodata}, retry={nodata}")
    if meta.band_count is not None and band_count is not None and meta.band_count != band_count:
        mismatches.append(f"band_count: original={meta.band_count}, retry={band_count}")
    if meta.data_type is not None and data_type is not None and meta.data_type != data_type:
        mismatches.append(f"data_type: original={meta.data_type}, retry={data_type}")
    if meta.ee_expression != ee_expression:
        meta_hash = meta.ee_expression_sha256
        retry_hash = _hash_expression(ee_expression)
        mismatches.append(
            f"ee_expression: original sha256={meta_hash[:16]}…, retry sha256={retry_hash[:16]}…"
        )
    if mismatches:
        bullets = "\n  - ".join(mismatches)
        raise ExportMetaMismatch(
            f"Retry args don't match the original export "
            f"(from {EXPORT_META_FILENAME}):\n  - {bullets}\n\n"
            "Proceeding would key new COGs to a different output grid "
            "than the existing ones. Fix the retry args to match the "
            f"original export, or delete {EXPORT_META_FILENAME} from "
            "the output directory to bypass this check (advanced; you "
            "must be sure the geometry is compatible)."
        )
