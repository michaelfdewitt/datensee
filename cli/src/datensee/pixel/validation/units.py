"""Output-unit mapping — bridges compute tiles to on-disk COG files.

The pipeline writes one COG per *output unit*: per compute tile in the
default mode (named from compute indices, sized ``width_px × height_px``),
or per distinct ``(out_row, out_col)`` group in two-tier mode (named
from output indices, sized ``OTS × OTS``, origin at local pixel
``(out_col·OTS, out_row·OTS)`` in the parent grid). The ``integrity``
check operates on units; ``pixels`` descends to member tiles, reading
each as a window of its unit's COG in two-tier mode.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import BaseModel

from datensee.config import PipelineConfig
from datensee.pixel.config import TileCoordinate

# On-disk COG filename pattern. ``%04d`` widens beyond 4 digits for
# indices >= 10000, hence ``\d{4,}``.
TILE_FILENAME_RE = re.compile(r"^tile_r(\d{4,})_c(\d{4,})\.tif$")

FAILURES_FILENAME = "_failures.json"
"""The pipeline's dead-letter journal, written next to the output COGs."""

# Shared SKIPPED message for configs whose tiles are externalized to a
# file — checks cannot enumerate output units without inline coordinates.
TILES_FILE_SKIP_MESSAGE = (
    "Tiles are externalized to a file (tile_grid.tiles_file); "
    "this check requires inline tile coordinates"
)


def unit_filename(key_row: int, key_col: int) -> str:
    """Canonical COG filename, e.g. ``tile_r0003_c0012.tif`` (``:04d`` widens like Java's)."""
    return f"tile_r{key_row:04d}_c{key_col:04d}.tif"


class OutputUnit(BaseModel):
    """One expected on-disk COG plus the compute tiles that feed it.

    ``key_row``/``key_col`` are the filename indices (compute ``row``/``col``
    in non-two-tier mode, ``out_row``/``out_col`` in two-tier mode); the origin fields
    are the unit's NW corner in local parent-grid pixels.
    """

    key_row: int
    key_col: int
    origin_col_px: int
    origin_row_px: int
    width_px: int
    height_px: int
    members: list[TileCoordinate]

    @property
    def key(self) -> tuple[int, int]:
        """Unit key ``(key_row, key_col)`` — matches the parsed filename."""
        return (self.key_row, self.key_col)

    @property
    def filename(self) -> str:
        """Canonical COG filename for this unit."""
        return unit_filename(self.key_row, self.key_col)


def is_m6(config: PipelineConfig) -> bool:
    """Whether the config selects two-tier output (grouped COGs)."""
    out_size = config.output.output_tile_size_pixels
    return out_size is not None and out_size > config.tile_grid.tile_size_pixels


def expected_output_units(config: PipelineConfig) -> list[OutputUnit] | None:
    """Map a config to the output units the pipeline writes.

    Returns ``None`` when tiles are externalized (``tile_grid.tiles is None``);
    callers should SKIP with :data:`TILES_FILE_SKIP_MESSAGE`.
    """
    tiles = config.tile_grid.tiles
    if tiles is None:
        return None

    if not is_m6(config):
        return [
            OutputUnit(
                key_row=t.row,
                key_col=t.col,
                origin_col_px=t.col_px,
                origin_row_px=t.row_px,
                width_px=t.width_px,
                height_px=t.height_px,
                members=[t],
            )
            for t in tiles
        ]

    out_size = config.output.output_tile_size_pixels
    assert out_size is not None  # is_m6 guarantees this
    groups: dict[tuple[int, int], list[TileCoordinate]] = {}
    for t in tiles:
        groups.setdefault((t.out_row, t.out_col), []).append(t)

    return [
        OutputUnit(
            key_row=out_row,
            key_col=out_col,
            origin_col_px=out_col * out_size,
            origin_row_px=out_row * out_size,
            width_px=out_size,
            height_px=out_size,
            members=members,
        )
        for (out_row, out_col), members in sorted(groups.items())
    ]


def unit_keys_on_disk(output_dir: Path) -> set[tuple[int, int]]:
    """Scan the output directory for COG files and parse their unit keys."""
    matches = (TILE_FILENAME_RE.match(p.name) for p in output_dir.glob("tile_r*_c*.tif"))
    return {(int(m.group(1)), int(m.group(2))) for m in matches if m}


def read_failure_keys(output_dir: Path) -> set[tuple[int, int]]:
    """Read ``_failures.json`` (NDJSON) and return ``(row, col)`` of failed tiles.

    An absent journal means zero failures. Malformed lines are skipped
    individually — one corrupt record (e.g. a partial flush at the end of
    a job) must not hide the valid entries above it.
    """
    failures_path = output_dir / FAILURES_FILENAME
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
        row, col = entry.get("row"), entry.get("col")
        if row is not None and col is not None:
            failed.add((int(row), int(col)))
    return failed
