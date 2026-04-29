"""Adaptive retry — read a failures journal, split or retry each entry.

A failure journal record (`FailedTileRecord` shape) is a superset of a
`TileCoordinate` record. The retry pipeline reads the journal, applies a
split-or-retry decision per entry, and emits a new tiles file that the
existing `tiles_file` input path can consume — so retries flow through
the same fetch / assemble / write pipeline as the original export.

Conservative defaults (see `docs/retry-with-journal.md`):

- Split allowlist: ``MEMORY_EXCEEDED`` and ``COMPUTATION_TIMEOUT`` only.
- Retry-same allowlist: ``RATE_LIMITED``, ``RETRYABLE_SERVER``, ``UNKNOWN``.
- Other kinds (``AUTH_ERROR``, ``FATAL_REQUEST``) are dropped from the
  retry stream and remain in the next round's failures journal.
- Max split depth: 2 (one root tile becomes at most 16 sub-tiles).
- Adaptive splitting is opt-in: this module isn't called from the
  default `datensee export` path. The caller (`datensee retry`) is the
  gate.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from datensee.config import TileCoordinate

# Quadrant index → (x_low, y_low) flags.
# 0=x_low/y_low, 1=x_high/y_low, 2=x_low/y_high, 3=x_high/y_high.
# Defined in bbox terms (not compass directions) so the encoding is
# stable across CRS axis orientations.
_QUADRANT_BBOX_FLAGS: dict[int, tuple[bool, bool]] = {
    0: (True, True),    # x-low / y-low
    1: (False, True),   # x-high / y-low
    2: (True, False),   # x-low / y-high
    3: (False, False),  # x-high / y-high
}

# Kinds that should trigger a quadtree split. Keep this set conservative
# — adding a kind here can cause cascades on transient infra failures.
SPLIT_ELIGIBLE_KINDS: frozenset[str] = frozenset(
    {"MEMORY_EXCEEDED", "COMPUTATION_TIMEOUT"}
)

# Kinds where we retry the same bbox after backoff. The pipeline already
# does its own per-attempt retry within a single fetch; this set is for
# tiles that exhausted that internal retry budget, where a fresh round
# (different worker, possibly different time) might succeed.
RETRY_SAME_KINDS: frozenset[str] = frozenset(
    {"RATE_LIMITED", "RETRYABLE_SERVER", "UNKNOWN"}
)

# Kinds we never retry — surface them to the user instead.
TERMINAL_KINDS: frozenset[str] = frozenset({"AUTH_ERROR", "FATAL_REQUEST"})

DEFAULT_MAX_DEPTH: int = 2


# Canonical journal_reason values — must stay in sync with
# FailedTileRecord.JOURNAL_REASON_* constants on the Java side. These
# describe *why a record is sitting in _failures.json* (the latest
# retry-policy verdict), distinct from `error_kind` (the EE-side
# classification of what went wrong on EE's end).
JOURNAL_REASON_FAILED: str = "failed"
JOURNAL_REASON_DEPTH_CAP: str = "depth_cap"
JOURNAL_REASON_TERMINAL: str = "terminal"
JOURNAL_REASON_UNKNOWN_KIND: str = "unknown_kind"

# Maps the action returned by `decide()` to the canonical journal_reason
# stamped on a carryover record. Only carryover actions appear here —
# `split` and `retry_same` records don't go to the journal; they go
# back into the fetch pipeline.
_ACTION_TO_JOURNAL_REASON: dict[str, str] = {
    "depth_cap": JOURNAL_REASON_DEPTH_CAP,
    "terminal": JOURNAL_REASON_TERMINAL,
    "unknown_kind": JOURNAL_REASON_UNKNOWN_KIND,
}


class JournalParseError(ValueError):
    """Raised when a journal line is malformed or missing required fields."""


@dataclass(frozen=True)
class RetryDecision:
    """Outcome of evaluating one journal entry against the policy."""

    # Children to feed back into the pipeline. Empty if the entry is
    # terminal (kind not in any allowlist) or at the depth cap.
    children: tuple[TileCoordinate, ...]

    # Why we made this decision. One of: "split", "retry_same",
    # "depth_cap", "terminal", "unknown_kind".
    action: str

    # Original journal entry, kept for re-emission into the next-round
    # journal when the action is non-progressing (depth_cap, terminal).
    record: dict


def _parse_record(line: str) -> dict:
    line = line.strip()
    if not line:
        raise JournalParseError("empty journal line")
    try:
        record = json.loads(line)
    except json.JSONDecodeError as exc:
        raise JournalParseError(f"invalid JSON: {exc}") from exc
    for required in ("x_min", "y_min", "x_max", "y_max", "row", "col"):
        if required not in record:
            raise JournalParseError(f"missing required field {required!r}: {line[:120]}")
    return record


def _record_to_tile(record: dict) -> TileCoordinate:
    """Reconstruct a TileCoordinate from a journal entry's TileCoordinate-shaped fields."""
    return TileCoordinate(
        x_min=float(record["x_min"]),
        y_min=float(record["y_min"]),
        x_max=float(record["x_max"]),
        y_max=float(record["y_max"]),
        row=int(record["row"]),
        col=int(record["col"]),
        out_row=int(record.get("out_row", record["row"])),
        out_col=int(record.get("out_col", record["col"])),
        lineage=list(record.get("lineage", [])),
    )


def split_tile(
    parent: TileCoordinate,
) -> tuple[TileCoordinate, TileCoordinate, TileCoordinate, TileCoordinate]:
    """Split a tile into its 4 quadrants by halving each axis.

    Children inherit ``(row, col, out_row, out_col)`` from the parent —
    they belong to the same output tile and the same root compute tile.
    Each child's lineage extends the parent's by one quadrant index.
    Bbox math is in CRS units so the encoding is independent of axis
    orientation: quadrant 0 is the x-low / y-low corner, etc.
    """
    x_mid = (parent.x_min + parent.x_max) / 2.0
    y_mid = (parent.y_min + parent.y_max) / 2.0
    children = []
    for q in range(4):
        x_low, y_low = _QUADRANT_BBOX_FLAGS[q]
        x_min = parent.x_min if x_low else x_mid
        x_max = x_mid if x_low else parent.x_max
        y_min = parent.y_min if y_low else y_mid
        y_max = y_mid if y_low else parent.y_max
        children.append(
            TileCoordinate(
                x_min=x_min,
                y_min=y_min,
                x_max=x_max,
                y_max=y_max,
                row=parent.row,
                col=parent.col,
                out_row=parent.out_row,
                out_col=parent.out_col,
                lineage=[*parent.lineage, q],
            )
        )
    return tuple(children)  # type: ignore[return-value]


def decide(
    record: dict,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    split_allowlist: frozenset[str] = SPLIT_ELIGIBLE_KINDS,
    retry_allowlist: frozenset[str] = RETRY_SAME_KINDS,
) -> RetryDecision:
    """Decide split-vs-retry-same-vs-drop for one journal entry.

    Args:
        record: Parsed journal record (dict).
        max_depth: Maximum total quadtree depth. A tile already at this
            depth gets ``action="depth_cap"`` and no children are emitted
            (the entry remains in the next round's failures journal).
        split_allowlist: Error kinds that trigger splitting. Default is
            EE-specific complexity signals only.
        retry_allowlist: Error kinds that retry the same bbox. Default
            is transient infrastructure failures.

    Returns:
        RetryDecision describing the outcome and any emitted children.
    """
    parent = _record_to_tile(record)
    kind = record.get("error_kind", "UNKNOWN")

    if kind in split_allowlist:
        if len(parent.lineage) >= max_depth:
            return RetryDecision((), "depth_cap", record)
        children = split_tile(parent)
        return RetryDecision(children, "split", record)
    if kind in retry_allowlist:
        return RetryDecision((parent,), "retry_same", record)
    if kind in TERMINAL_KINDS:
        return RetryDecision((), "terminal", record)
    return RetryDecision((), "unknown_kind", record)


@dataclass(frozen=True)
class RetryPlan:
    """Result of planning a retry round.

    ``next_tiles`` is a list of TileCoordinate ready to feed into the
    pipeline via ``tiles_file``. ``carryover`` contains the original
    journal records that didn't make progress this round (depth-capped
    or terminal) and should be re-emitted into the next failures journal.
    ``stats`` is a dict of action → count for reporting.
    """

    next_tiles: list[TileCoordinate]
    carryover: list[dict]
    stats: dict[str, int]


def plan_retry(
    journal_records: Iterable[dict],
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    split_allowlist: frozenset[str] = SPLIT_ELIGIBLE_KINDS,
    retry_allowlist: frozenset[str] = RETRY_SAME_KINDS,
) -> RetryPlan:
    """Apply :func:`decide` to every record in a journal, return the plan.

    Carryover records are stamped (in place, on a shallow copy) with the
    appropriate ``journal_reason`` — ``depth_cap`` / ``terminal`` /
    ``unknown_kind`` — so that when the retry CLI appends them onto
    ``_failures.json``, a downstream reader can tell at a glance why
    each entry is sitting in the journal.
    """
    next_tiles: list[TileCoordinate] = []
    carryover: list[dict] = []
    stats: dict[str, int] = {}
    for record in journal_records:
        d = decide(
            record,
            max_depth=max_depth,
            split_allowlist=split_allowlist,
            retry_allowlist=retry_allowlist,
        )
        stats[d.action] = stats.get(d.action, 0) + 1
        if d.children:
            next_tiles.extend(d.children)
        else:
            stamped = dict(record)
            stamped["journal_reason"] = _ACTION_TO_JOURNAL_REASON.get(
                d.action, JOURNAL_REASON_FAILED
            )
            carryover.append(stamped)
    return RetryPlan(next_tiles=next_tiles, carryover=carryover, stats=stats)


def read_journal(path: Path) -> list[dict]:
    """Load an NDJSON failures journal into a list of records."""
    records: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            records.append(_parse_record(line))
    return records


def write_tiles_file(tiles: Iterable[TileCoordinate], path: Path) -> None:
    """Write a list of TileCoordinates as NDJSON to ``path``.

    Format matches the schema that ``TileCoordinateParser`` (Java) reads
    from ``tile_grid.tiles_file``: one JSON object per line with the
    full TileCoordinate fields (bbox, row, col, out_row, out_col, lineage).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for t in tiles:
            f.write(t.model_dump_json())
            f.write("\n")
