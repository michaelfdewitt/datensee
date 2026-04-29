"""Tests for the adaptive retry split-vs-retry decision logic.

Pins the conservative defaults documented in
``docs/retry-with-journal.md`` — only ``MEMORY_EXCEEDED`` and
``COMPUTATION_TIMEOUT`` split, transient infra retries the same bbox,
auth/fatal kinds drop out, and the depth cap stops cascades.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from datensee.config import TileCoordinate
from datensee.retry import (
    DEFAULT_MAX_DEPTH,
    JournalParseError,
    decide,
    plan_retry,
    read_journal,
    split_tile,
    write_tiles_file,
)


def _record(
    *,
    error_kind: str = "MEMORY_EXCEEDED",
    lineage: list[int] | None = None,
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 100.0, 100.0),
    row: int = 0,
    col: int = 0,
    out_row: int = 0,
    out_col: int = 0,
) -> dict:
    return {
        "x_min": bbox[0],
        "y_min": bbox[1],
        "x_max": bbox[2],
        "y_max": bbox[3],
        "row": row,
        "col": col,
        "out_row": out_row,
        "out_col": out_col,
        "lineage": list(lineage or []),
        "error_kind": error_kind,
        "attempts": 5,
    }


# ---------------------------------------------------------------------------
# split_tile — pure geometry
# ---------------------------------------------------------------------------


class TestSplitTile:
    def test_quadrant_zero_is_x_low_y_low(self) -> None:
        parent = TileCoordinate(x_min=0, y_min=0, x_max=100, y_max=100, row=0, col=0)
        q0, q1, q2, q3 = split_tile(parent)
        assert (q0.x_min, q0.y_min, q0.x_max, q0.y_max) == (0, 0, 50, 50)
        # q1 = x-high / y-low
        assert (q1.x_min, q1.y_min, q1.x_max, q1.y_max) == (50, 0, 100, 50)
        # q2 = x-low / y-high
        assert (q2.x_min, q2.y_min, q2.x_max, q2.y_max) == (0, 50, 50, 100)
        # q3 = x-high / y-high
        assert (q3.x_min, q3.y_min, q3.x_max, q3.y_max) == (50, 50, 100, 100)

    def test_children_inherit_row_col_out_row_out_col(self) -> None:
        parent = TileCoordinate(
            x_min=0, y_min=0, x_max=100, y_max=100,
            row=7, col=11, out_row=1, out_col=2,
        )
        for child in split_tile(parent):
            assert child.row == 7 and child.col == 11
            assert child.out_row == 1 and child.out_col == 2

    def test_children_lineage_extends_parent_with_quadrant_index(self) -> None:
        parent = TileCoordinate(
            x_min=0, y_min=0, x_max=100, y_max=100,
            row=0, col=0, lineage=[3, 1],
        )
        children = split_tile(parent)
        for q in range(4):
            assert children[q].lineage == [3, 1, q]

    def test_split_is_exact_geometric_bisection(self) -> None:
        parent = TileCoordinate(
            x_min=-122.5, y_min=37.5, x_max=-122.4, y_max=37.6, row=0, col=0
        )
        children = split_tile(parent)
        # Union of children covers the parent exactly.
        x_mins = {c.x_min for c in children}
        x_maxs = {c.x_max for c in children}
        y_mins = {c.y_min for c in children}
        y_maxs = {c.y_max for c in children}
        assert x_mins == {parent.x_min, (parent.x_min + parent.x_max) / 2}
        assert x_maxs == {(parent.x_min + parent.x_max) / 2, parent.x_max}
        assert y_mins == {parent.y_min, (parent.y_min + parent.y_max) / 2}
        assert y_maxs == {(parent.y_min + parent.y_max) / 2, parent.y_max}


# ---------------------------------------------------------------------------
# decide — split-vs-retry-same-vs-drop
# ---------------------------------------------------------------------------


class TestDecide:
    def test_memory_exceeded_splits(self) -> None:
        d = decide(_record(error_kind="MEMORY_EXCEEDED"))
        assert d.action == "split"
        assert len(d.children) == 4
        for child in d.children:
            assert child.lineage in ([0], [1], [2], [3])

    def test_computation_timeout_splits(self) -> None:
        d = decide(_record(error_kind="COMPUTATION_TIMEOUT"))
        assert d.action == "split"
        assert len(d.children) == 4

    def test_rate_limited_retries_same_bbox(self) -> None:
        d = decide(_record(error_kind="RATE_LIMITED"))
        assert d.action == "retry_same"
        assert len(d.children) == 1
        # Same bbox, lineage unchanged.
        assert d.children[0].lineage == []

    def test_retryable_server_retries_same(self) -> None:
        d = decide(_record(error_kind="RETRYABLE_SERVER"))
        assert d.action == "retry_same"

    def test_unknown_retries_same(self) -> None:
        d = decide(_record(error_kind="UNKNOWN"))
        assert d.action == "retry_same"

    def test_auth_error_is_terminal(self) -> None:
        d = decide(_record(error_kind="AUTH_ERROR"))
        assert d.action == "terminal"
        assert len(d.children) == 0

    def test_fatal_request_is_terminal(self) -> None:
        d = decide(_record(error_kind="FATAL_REQUEST"))
        assert d.action == "terminal"
        assert len(d.children) == 0

    def test_unrecognized_kind_drops(self) -> None:
        d = decide(_record(error_kind="SOMETHING_NEW"))
        assert d.action == "unknown_kind"
        assert len(d.children) == 0

    def test_depth_cap_prevents_further_split(self) -> None:
        # At max_depth=2, a record with lineage of length 2 stops splitting.
        d = decide(
            _record(error_kind="MEMORY_EXCEEDED", lineage=[0, 1]),
            max_depth=2,
        )
        assert d.action == "depth_cap"
        assert len(d.children) == 0

    def test_below_depth_cap_still_splits(self) -> None:
        d = decide(
            _record(error_kind="MEMORY_EXCEEDED", lineage=[2]),
            max_depth=2,
        )
        assert d.action == "split"
        for child in d.children:
            assert len(child.lineage) == 2  # parent depth 1 → child depth 2

    def test_default_max_depth_is_two(self) -> None:
        # Sanity check: the documented default matches.
        assert DEFAULT_MAX_DEPTH == 2

    def test_custom_split_allowlist_overrides_default(self) -> None:
        # If a caller wants to also split on RETRYABLE_SERVER (NOT recommended),
        # they can override the allowlist.
        d = decide(
            _record(error_kind="RETRYABLE_SERVER"),
            split_allowlist=frozenset({"RETRYABLE_SERVER"}),
        )
        assert d.action == "split"


# ---------------------------------------------------------------------------
# plan_retry — apply decide() to a stream
# ---------------------------------------------------------------------------


class TestPlanRetry:
    def test_mixed_journal_routes_records_correctly(self) -> None:
        records = [
            _record(error_kind="MEMORY_EXCEEDED"),                   # split → 4 children
            _record(error_kind="RATE_LIMITED"),                      # retry → 1 child
            _record(error_kind="AUTH_ERROR"),                        # terminal → 0
            _record(error_kind="MEMORY_EXCEEDED", lineage=[0, 1]),   # depth_cap → 0
        ]
        plan = plan_retry(records)
        assert plan.stats["split"] == 1
        assert plan.stats["retry_same"] == 1
        assert plan.stats["terminal"] == 1
        assert plan.stats["depth_cap"] == 1
        assert len(plan.next_tiles) == 5  # 4 from split + 1 from retry
        assert len(plan.carryover) == 2   # terminal + depth_cap


# ---------------------------------------------------------------------------
# Journal I/O
# ---------------------------------------------------------------------------


class TestJournalIO:
    def test_read_skips_blank_lines_and_parses_records(self, tmp_path: Path) -> None:
        path = tmp_path / "_failures.json"
        path.write_text(
            json.dumps(_record(error_kind="MEMORY_EXCEEDED")) + "\n"
            "\n"  # blank line should be skipped
            + json.dumps(_record(error_kind="RATE_LIMITED")) + "\n"
        )
        records = read_journal(path)
        assert len(records) == 2
        assert records[0]["error_kind"] == "MEMORY_EXCEEDED"

    def test_read_rejects_malformed_json(self, tmp_path: Path) -> None:
        path = tmp_path / "_failures.json"
        path.write_text("not json\n")
        with pytest.raises(JournalParseError):
            read_journal(path)

    def test_read_rejects_missing_required_field(self, tmp_path: Path) -> None:
        path = tmp_path / "_failures.json"
        path.write_text(json.dumps({"x_min": 0, "y_min": 0, "x_max": 1}) + "\n")
        with pytest.raises(JournalParseError, match="missing required field"):
            read_journal(path)

    def test_write_round_trips_through_tilecoordinate(self, tmp_path: Path) -> None:
        tiles = [
            TileCoordinate(x_min=0, y_min=0, x_max=10, y_max=10, row=0, col=0),
            TileCoordinate(
                x_min=10, y_min=0, x_max=20, y_max=10,
                row=0, col=1, out_row=0, out_col=0, lineage=[2],
            ),
        ]
        path = tmp_path / "tiles.json"
        write_tiles_file(tiles, path)

        # The written file should be one JSON object per line, parseable
        # by a TileCoordinate consumer.
        lines = path.read_text().strip().splitlines()
        assert len(lines) == 2
        for line, original in zip(lines, tiles, strict=True):
            parsed = TileCoordinate.model_validate_json(line)
            assert parsed.x_min == original.x_min
            assert parsed.lineage == original.lineage
