"""Tests for the export-metadata sidecar.

Pins the round-trip behavior and the mismatch-detection contract.
GCS-backed paths are tested via api.export integration, not here —
this file stays local-filesystem only to avoid mocking the cloud
client for behavior the local path already exercises.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from datensee.meta import (
    EXPORT_META_FILENAME,
    ExportMeta,
    ExportMetaMismatch,
    build_meta,
    read_meta,
    verify_retry_compatibility,
    write_meta,
)

# Plausible canonical export shape used as the "original" across tests.
_ORIGINAL_KW = {
    "crs": "EPSG:4326",
    "scale_meters": 30.0,
    "tile_size_pixels": 512,
    "output_tile_size_pixels": 2048,
    "gee_project": "datensee-testing",
    "ee_expression": '{"result":"0","values":{"0":{"constantValue":1}}}',
}


def test_build_meta_round_trips_through_disk(tmp_path: Path) -> None:
    meta = build_meta(**_ORIGINAL_KW)
    write_meta(str(tmp_path), meta)

    persisted_path = tmp_path / EXPORT_META_FILENAME
    assert persisted_path.exists()

    reloaded = read_meta(str(tmp_path))
    assert reloaded == meta


def test_read_meta_returns_none_when_absent(tmp_path: Path) -> None:
    assert read_meta(str(tmp_path)) is None


def test_verify_passes_on_exact_match(tmp_path: Path) -> None:
    meta = build_meta(**_ORIGINAL_KW)
    # Same args as the original: no exception should be raised.
    verify_retry_compatibility(meta, **_ORIGINAL_KW)


@pytest.mark.parametrize(
    "field,changed_value",
    [
        ("crs", "EPSG:32610"),
        ("scale_meters", 31.0),
        ("tile_size_pixels", 256),
        ("output_tile_size_pixels", 4096),
        ("gee_project", "different-project"),
        (
            "ee_expression",
            '{"result":"0","values":{"0":{"constantValue":2}}}',
        ),
    ],
)
def test_verify_raises_on_each_field_mismatch(field: str, changed_value: object) -> None:
    meta = build_meta(**_ORIGINAL_KW)
    args = dict(_ORIGINAL_KW)
    args[field] = changed_value
    with pytest.raises(ExportMetaMismatch) as exc_info:
        verify_retry_compatibility(meta, **args)  # type: ignore[arg-type]
    # Error message must name the offending field so the user knows what
    # to fix.
    assert field in str(exc_info.value)


def test_verify_collects_all_mismatches_into_one_error() -> None:
    meta = build_meta(**_ORIGINAL_KW)
    args = dict(_ORIGINAL_KW)
    args["scale_meters"] = 31.0
    args["gee_project"] = "different-project"
    with pytest.raises(ExportMetaMismatch) as exc_info:
        verify_retry_compatibility(meta, **args)  # type: ignore[arg-type]
    msg = str(exc_info.value)
    assert "scale_meters" in msg
    assert "gee_project" in msg


def test_meta_omits_full_expression_from_hash_property() -> None:
    """The hash property is derived from the expression; recomputing
    it on demand keeps the persisted JSON minimal."""
    meta = ExportMeta(**_ORIGINAL_KW)
    assert len(meta.ee_expression_sha256) == 64  # sha256 hex


def test_output_tile_size_none_round_trips(tmp_path: Path) -> None:
    """A non-two-tier export persists output_tile_size_pixels=None and read
    must surface it back as None (not a missing key, not zero)."""
    args = dict(_ORIGINAL_KW)
    args["output_tile_size_pixels"] = None
    meta = build_meta(**args)
    write_meta(str(tmp_path), meta)
    reloaded = read_meta(str(tmp_path))
    assert reloaded is not None
    assert reloaded.output_tile_size_pixels is None


def test_legacy_nanosecond_snapshot_time_is_migrated(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Pre-fix meta files stored ``snapshot_time`` in nanoseconds. A
    retry today must still work: read should silently divide by 1000
    and warn, so the migrated value lands safely under
    ``pin_expression``'s out-of-range guard."""
    args = dict(_ORIGINAL_KW)
    args["snapshot_time"] = 1_778_588_808_169_445_000  # legacy nanos
    raw_path = tmp_path / EXPORT_META_FILENAME
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    # Write directly: build_meta would route through the validator and
    # migrate before persisting, which isn't the legacy path we test.
    raw_path.write_text(
        ExportMeta.model_construct(**args).model_dump_json(),
        encoding="utf-8",
    )

    with caplog.at_level("WARNING"):
        reloaded = read_meta(str(tmp_path))

    assert reloaded is not None
    assert reloaded.snapshot_time == 1_778_588_808_169_445  # micros
    assert any("nanoseconds (legacy" in r.message for r in caplog.records)


def test_microsecond_snapshot_time_round_trips_unchanged(tmp_path: Path) -> None:
    args = dict(_ORIGINAL_KW)
    args["snapshot_time"] = 1_778_588_808_169_445  # already micros
    meta = build_meta(**args)
    write_meta(str(tmp_path), meta)
    reloaded = read_meta(str(tmp_path))
    assert reloaded is not None
    assert reloaded.snapshot_time == 1_778_588_808_169_445
