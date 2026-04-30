"""Assembly and accounting checks — E08, E10.

E08: tiles_on_disk + tiles_in_failures == tiles_in_config.
E10: Total output size within 0.2x–5x of raw (uncompressed) prediction.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from datensee.config import PipelineConfig
from datensee.validation.catalog import CheckID
from datensee.validation.report import CheckResult, CheckStatus

# Regex to extract (row, col) from tile filenames like tile_r0003_c0012.tif
_TILE_FILENAME_RE = re.compile(r"^tile_r(\d{4})_c(\d{4})\.tif$")


def _tiles_on_disk(output_dir: Path) -> set[tuple[int, int]]:
    """Scan output directory for tile files and return their (row, col) coordinates."""
    found: set[tuple[int, int]] = set()
    for path in output_dir.glob("tile_r*_c*.tif"):
        m = _TILE_FILENAME_RE.match(path.name)
        if m:
            found.add((int(m.group(1)), int(m.group(2))))
    return found


def _tiles_in_failures(output_dir: Path) -> set[tuple[int, int]]:
    """Read _failures.json (NDJSON) and return (row, col) of failed tiles.

    Skips malformed lines individually rather than dropping the whole
    journal — the whole point of NDJSON is per-record robustness, and
    one corrupt record (e.g. a partial flush at the end of a job)
    shouldn't hide thousands of valid entries above it.
    """
    failures_path = output_dir / "_failures.json"
    if not failures_path.exists():
        return set()

    try:
        text = failures_path.read_text()
    except OSError:
        return set()

    failed: set[tuple[int, int]] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        row = entry.get("row")
        col = entry.get("col")
        if row is not None and col is not None:
            failed.add((int(row), int(col)))
    return failed


def _tiles_in_config(config: PipelineConfig) -> set[tuple[int, int]]:
    """Extract (row, col) set from pipeline config."""
    tiles = config.tile_grid.tiles or []
    return {(t.row, t.col) for t in tiles}


def check_e08_failure_accounting(
    output_dir: Path,
    config: PipelineConfig,
) -> CheckResult:
    """E08: Verify tiles_on_disk + tiles_in_failures == tiles_in_config.

    Every tile in the config must be accounted for — either as a file on disk
    or as an entry in _failures.json. No tiles should be silently lost.
    """
    expected = _tiles_in_config(config)
    on_disk = _tiles_on_disk(output_dir)
    in_failures = _tiles_in_failures(output_dir)

    accounted = on_disk | in_failures
    unaccounted = expected - accounted
    unexpected = accounted - expected

    if not unaccounted and not unexpected:
        return CheckResult(
            check_id=CheckID.E08,
            status=CheckStatus.PASSED,
            message=(
                f"All {len(expected)} tiles accounted for "
                f"({len(on_disk)} on disk, {len(in_failures)} in failures)"
            ),
        )

    parts: list[str] = []
    if unaccounted:
        parts.append(f"{len(unaccounted)} tiles missing from both disk and failures")
    if unexpected:
        parts.append(f"{len(unexpected)} tiles on disk/failures but not in config")

    return CheckResult(
        check_id=CheckID.E08,
        status=CheckStatus.FAILED,
        message="; ".join(parts),
        details={
            "unaccounted": sorted(unaccounted)[:20],
            "unexpected": sorted(unexpected)[:20],
            "on_disk": len(on_disk),
            "in_failures": len(in_failures),
            "in_config": len(expected),
        },
    )


# ---------------------------------------------------------------------------
# E10: Output Size Plausibility
# ---------------------------------------------------------------------------

_SIZE_LOW_FACTOR = 0.2
_SIZE_HIGH_FACTOR = 5.0


def check_e10_size_plausibility(
    output_dir: Path,
    config: PipelineConfig,
) -> CheckResult:
    """E10: Verify total output size is plausible vs. raw (uncompressed) prediction.

    Actual compressed size should be within 0.2x–5x of the raw size. Wide bounds
    account for compression variability and nodata regions.
    """
    predicted_bytes = config.raw_output_bytes

    if predicted_bytes == 0:
        return CheckResult(
            check_id=CheckID.E10,
            status=CheckStatus.SKIPPED,
            message="Cannot estimate size (tile count unknown)",
        )

    actual_bytes = sum(p.stat().st_size for p in output_dir.glob("tile_r*_c*.tif") if p.is_file())

    low = int(predicted_bytes * _SIZE_LOW_FACTOR)
    high = int(predicted_bytes * _SIZE_HIGH_FACTOR)
    ratio = actual_bytes / predicted_bytes if predicted_bytes > 0 else 0.0

    if low <= actual_bytes <= high:
        return CheckResult(
            check_id=CheckID.E10,
            status=CheckStatus.PASSED,
            message=(
                f"Output size {_fmt(actual_bytes)} is {ratio:.1f}x "
                f"of predicted {_fmt(predicted_bytes)}"
            ),
            details={
                "actual_bytes": actual_bytes,
                "predicted_bytes": predicted_bytes,
                "ratio": ratio,
            },
        )

    direction = "smaller" if actual_bytes < low else "larger"
    return CheckResult(
        check_id=CheckID.E10,
        status=CheckStatus.FAILED,
        message=(
            f"Output size {_fmt(actual_bytes)} is {ratio:.1f}x of predicted "
            f"{_fmt(predicted_bytes)} — {direction} than expected "
            f"(bounds: {_SIZE_LOW_FACTOR}x–{_SIZE_HIGH_FACTOR}x)"
        ),
        details={
            "actual_bytes": actual_bytes,
            "predicted_bytes": predicted_bytes,
            "ratio": ratio,
            "bounds": [_SIZE_LOW_FACTOR, _SIZE_HIGH_FACTOR],
        },
    )


def _fmt(n: int) -> str:
    """Human-readable byte size."""
    if n < 1024:
        return f"{n} B"
    if n < 1024**2:
        return f"{n / 1024:.1f} KB"
    if n < 1024**3:
        return f"{n / 1024**2:.1f} MB"
    return f"{n / 1024**3:.2f} GB"
