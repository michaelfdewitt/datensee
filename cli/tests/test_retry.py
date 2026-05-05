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
    JOURNAL_REASON_DEPTH_CAP,
    JOURNAL_REASON_SPLIT_DISABLED,
    JOURNAL_REASON_TERMINAL,
    JOURNAL_REASON_UNKNOWN_KIND,
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
    col_px: int = 0,
    row_px: int = 0,
    width_px: int = 512,
    height_px: int = 512,
    row: int = 0,
    col: int = 0,
    out_row: int = 0,
    out_col: int = 0,
) -> dict:
    return {
        "col_px": col_px,
        "row_px": row_px,
        "width_px": width_px,
        "height_px": height_px,
        "row": row,
        "col": col,
        "out_row": out_row,
        "out_col": out_col,
        "lineage": list(lineage or []),
        "error_kind": error_kind,
        "attempts": 5,
    }


# ---------------------------------------------------------------------------
# split_tile — pure pixel geometry
# ---------------------------------------------------------------------------


class TestSplitTile:
    def test_quadrant_zero_is_x_low_y_low(self) -> None:
        # Parent at origin, 100×100 px in pixel space — y-low corresponds
        # to LARGER row_px (rows count downward).
        parent = TileCoordinate(
            col_px=0,
            row_px=0,
            width_px=100,
            height_px=100,
            row=0,
            col=0,
        )
        q0, q1, q2, q3 = split_tile(parent)
        # q0 = x-low / y-low → west, south → col_px=0, row_px=50
        assert (q0.col_px, q0.row_px, q0.width_px, q0.height_px) == (0, 50, 50, 50)
        # q1 = x-high / y-low → east, south → col_px=50, row_px=50
        assert (q1.col_px, q1.row_px, q1.width_px, q1.height_px) == (50, 50, 50, 50)
        # q2 = x-low / y-high → west, north → col_px=0, row_px=0
        assert (q2.col_px, q2.row_px, q2.width_px, q2.height_px) == (0, 0, 50, 50)
        # q3 = x-high / y-high → east, north → col_px=50, row_px=0
        assert (q3.col_px, q3.row_px, q3.width_px, q3.height_px) == (50, 0, 50, 50)

    def test_children_inherit_row_col_out_row_out_col(self) -> None:
        parent = TileCoordinate(
            col_px=0,
            row_px=0,
            width_px=512,
            height_px=512,
            row=7,
            col=11,
            out_row=1,
            out_col=2,
        )
        for child in split_tile(parent):
            assert child.row == 7 and child.col == 11
            assert child.out_row == 1 and child.out_col == 2

    def test_children_lineage_extends_parent_with_quadrant_index(self) -> None:
        parent = TileCoordinate(
            col_px=0,
            row_px=0,
            width_px=512,
            height_px=512,
            row=0,
            col=0,
            lineage=[3, 1],
        )
        children = split_tile(parent)
        for q in range(4):
            assert children[q].lineage == [3, 1, q]

    def test_split_is_exact_pixel_bisection(self) -> None:
        parent = TileCoordinate(
            col_px=128,
            row_px=64,
            width_px=256,
            height_px=256,
            row=0,
            col=0,
        )
        children = split_tile(parent)
        # Union covers the parent: each axis split exactly into two halves.
        col_lefts = sorted({c.col_px for c in children})
        col_rights = sorted({c.col_px + c.width_px for c in children})
        assert col_lefts == [parent.col_px, parent.col_px + parent.width_px // 2]
        assert col_rights == [
            parent.col_px + parent.width_px // 2,
            parent.col_px + parent.width_px,
        ]
        row_tops = sorted({c.row_px for c in children})
        row_bottoms = sorted({c.row_px + c.height_px for c in children})
        assert row_tops == [parent.row_px, parent.row_px + parent.height_px // 2]
        assert row_bottoms == [
            parent.row_px + parent.height_px // 2,
            parent.row_px + parent.height_px,
        ]

    def test_split_rejects_odd_dimensions(self) -> None:
        parent = TileCoordinate(
            col_px=0,
            row_px=0,
            width_px=7,
            height_px=7,
            row=0,
            col=0,
        )
        with pytest.raises(ValueError, match="even width_px and height_px"):
            split_tile(parent)


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
            assert len(child.lineage) == 2

    def test_default_max_depth_is_two(self) -> None:
        assert DEFAULT_MAX_DEPTH == 2

    def test_custom_split_allowlist_overrides_default(self) -> None:
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
            _record(error_kind="MEMORY_EXCEEDED"),
            _record(error_kind="RATE_LIMITED"),
            _record(error_kind="AUTH_ERROR"),
            _record(error_kind="MEMORY_EXCEEDED", lineage=[0, 1]),
        ]
        plan = plan_retry(records)
        assert plan.stats["split"] == 1
        assert plan.stats["retry_same"] == 1
        assert plan.stats["terminal"] == 1
        assert plan.stats["depth_cap"] == 1
        assert len(plan.next_tiles) == 5
        assert len(plan.carryover) == 2

    def test_carryover_records_get_stamped_with_journal_reason(self) -> None:
        records = [
            _record(error_kind="AUTH_ERROR"),
            _record(error_kind="MEMORY_EXCEEDED", lineage=[0, 1]),
            _record(error_kind="SOMETHING_NEW"),
        ]
        plan = plan_retry(records)
        by_kind = {r["error_kind"]: r for r in plan.carryover}
        assert by_kind["AUTH_ERROR"]["journal_reason"] == JOURNAL_REASON_TERMINAL
        assert by_kind["MEMORY_EXCEEDED"]["journal_reason"] == JOURNAL_REASON_DEPTH_CAP
        assert by_kind["SOMETHING_NEW"]["journal_reason"] == JOURNAL_REASON_UNKNOWN_KIND

    def test_carryover_does_not_mutate_input_records(self) -> None:
        original = _record(error_kind="AUTH_ERROR")
        before = dict(original)
        plan_retry([original])
        assert original == before

    def test_allow_split_false_demotes_split_eligible_to_carryover(self) -> None:
        records = [
            _record(error_kind="MEMORY_EXCEEDED"),
            _record(error_kind="COMPUTATION_TIMEOUT"),
            _record(error_kind="RATE_LIMITED"),
        ]
        plan = plan_retry(records, allow_split=False)
        assert plan.stats.get("split", 0) == 0
        assert plan.stats["split_disabled"] == 2
        assert plan.stats["retry_same"] == 1
        kinds_in_carryover = {r["error_kind"] for r in plan.carryover}
        assert kinds_in_carryover == {"MEMORY_EXCEEDED", "COMPUTATION_TIMEOUT"}
        for record in plan.carryover:
            assert record["journal_reason"] == JOURNAL_REASON_SPLIT_DISABLED
        assert len(plan.next_tiles) == 1


# ---------------------------------------------------------------------------
# Journal I/O
# ---------------------------------------------------------------------------


class TestJournalIO:
    def test_read_skips_blank_lines_and_parses_records(self, tmp_path: Path) -> None:
        path = tmp_path / "_failures.json"
        path.write_text(
            json.dumps(_record(error_kind="MEMORY_EXCEEDED")) + "\n"
            "\n" + json.dumps(_record(error_kind="RATE_LIMITED")) + "\n"
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
        path.write_text(json.dumps({"col_px": 0, "row_px": 0, "width_px": 1}) + "\n")
        with pytest.raises(JournalParseError, match="missing required field"):
            read_journal(path)

    def test_write_round_trips_through_tilecoordinate(self, tmp_path: Path) -> None:
        tiles = [
            TileCoordinate(
                col_px=0,
                row_px=0,
                width_px=512,
                height_px=512,
                row=0,
                col=0,
            ),
            TileCoordinate(
                col_px=512,
                row_px=0,
                width_px=512,
                height_px=512,
                row=0,
                col=1,
                out_row=0,
                out_col=0,
                lineage=[2],
            ),
        ]
        path = tmp_path / "tiles.json"
        write_tiles_file(tiles, path)

        lines = path.read_text().strip().splitlines()
        assert len(lines) == 2
        for line, original in zip(lines, tiles, strict=True):
            parsed = TileCoordinate.model_validate_json(line)
            assert parsed.col_px == original.col_px
            assert parsed.lineage == original.lineage


# ---------------------------------------------------------------------------
# api.retry — carryover merge into _failures.json
# ---------------------------------------------------------------------------


class TestApiRetryCarryoverMerge:
    """End-to-end: api.retry() must append carryover (terminal + depth-cap)
    records back into _failures.json so the journal stays the canonical
    view of "what's still stuck" between retry rounds.
    """

    def test_terminal_kinds_appear_in_failures_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from datensee import api

        journal = tmp_path / "_failures.json"
        terminal_record = _record(
            error_kind="AUTH_ERROR",
            col_px=0,
            row_px=0,
            width_px=512,
            height_px=512,
            row=0,
            col=0,
        )
        memory_record = _record(
            error_kind="MEMORY_EXCEEDED",
            col_px=0,
            row_px=0,
            width_px=512,
            height_px=512,
            row=0,
            col=0,
        )
        journal.write_text(json.dumps(memory_record) + "\n" + json.dumps(terminal_record) + "\n")

        new_pipeline_failure = _record(
            error_kind="RATE_LIMITED",
            col_px=0,
            row_px=0,
            width_px=256,
            height_px=256,
            row=0,
            col=0,
            lineage=[0],
        )

        def fake_submit(*_args, **_kwargs):
            (tmp_path / "_failures.json").write_text(json.dumps(new_pipeline_failure) + "\n")
            return None

        from datensee import notebook
        from datensee import submit as submit_mod

        monkeypatch.setattr(submit_mod, "submit_job", fake_submit)
        monkeypatch.setattr(notebook, "ensure_jar", lambda: tmp_path / "stub.jar")
        monkeypatch.setattr(notebook, "ensure_auth", lambda: None)

        result = api.retry(
            journal=journal,
            ee_expression='{"result":"0","values":{"0":{"constantValue":1}}}',
            project="test-project",
            output=str(tmp_path),
            runner="local",
            tile_size=512,
            output_tile_size=1024,
            max_depth=2,
        )

        assert result.stats.get("split") == 1
        assert result.stats.get("terminal") == 1
        assert result.next_tiles_count == 4
        assert result.carryover_count == 1

        merged_lines = (tmp_path / "_failures.json").read_text().strip().splitlines()
        merged = [json.loads(line) for line in merged_lines]
        kinds = sorted(r["error_kind"] for r in merged)
        assert kinds == ["AUTH_ERROR", "RATE_LIMITED"], (
            f"Expected the new pipeline failure plus the carried-over AUTH_ERROR, got {kinds}"
        )
        by_kind = {r["error_kind"]: r for r in merged}
        assert by_kind["AUTH_ERROR"]["journal_reason"] == JOURNAL_REASON_TERMINAL

    def test_depth_capped_records_appear_in_failures_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from datensee import api

        capped_record = _record(error_kind="MEMORY_EXCEEDED", lineage=[0, 1])
        progressing_record = _record(error_kind="RATE_LIMITED")

        journal = tmp_path / "_failures.json"
        journal.write_text(json.dumps(capped_record) + "\n" + json.dumps(progressing_record) + "\n")

        def fake_submit(*_args, **_kwargs):
            (tmp_path / "_failures.json").write_text("")
            return None

        from datensee import notebook
        from datensee import submit as submit_mod

        monkeypatch.setattr(submit_mod, "submit_job", fake_submit)
        monkeypatch.setattr(notebook, "ensure_jar", lambda: tmp_path / "stub.jar")
        monkeypatch.setattr(notebook, "ensure_auth", lambda: None)

        result = api.retry(
            journal=journal,
            ee_expression='{"result":"0","values":{"0":{"constantValue":1}}}',
            project="test-project",
            output=str(tmp_path),
            runner="local",
            tile_size=512,
            output_tile_size=1024,
            max_depth=2,
        )

        assert result.stats.get("depth_cap") == 1
        assert result.carryover_count == 1

        merged_lines = (tmp_path / "_failures.json").read_text().strip().splitlines()
        merged = [json.loads(line) for line in merged_lines]
        assert len(merged) == 1
        assert merged[0]["error_kind"] == "MEMORY_EXCEEDED"
        assert merged[0]["lineage"] == [0, 1]
        assert merged[0]["journal_reason"] == JOURNAL_REASON_DEPTH_CAP


class TestApiRetryReadsFromMeta:
    """When _export_meta.json is present, retry needs no shape args."""

    _EXPRESSION = '{"result":"0","values":{"0":{"constantValue":1}}}'

    def _stage_export(self, tmp_path: Path) -> None:
        from datensee.meta import build_meta, write_meta

        meta = build_meta(
            crs="EPSG:4326",
            scale_meters=30.0,
            tile_size_pixels=512,
            output_tile_size_pixels=1024,
            gee_project="staged-project",
            ee_expression=self._EXPRESSION,
        )
        write_meta(str(tmp_path), meta)

    def test_retry_with_only_output_path_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from datensee import api, notebook
        from datensee import submit as submit_mod

        self._stage_export(tmp_path)
        record = _record(error_kind="RATE_LIMITED")
        (tmp_path / "_failures.json").write_text(json.dumps(record) + "\n")

        captured: dict[str, object] = {}

        def fake_submit(config, **_kwargs):
            captured["ee_expression"] = config.ee_expression
            captured["gee_project"] = config.gee_project
            captured["crs"] = config.tile_grid.crs
            captured["pixel_size"] = config.tile_grid.pixel_size
            captured["tile_size_pixels"] = config.tile_grid.tile_size_pixels
            captured["output_tile_size_pixels"] = config.output.output_tile_size_pixels
            (tmp_path / "_failures.json").write_text("")
            return None

        monkeypatch.setattr(submit_mod, "submit_job", fake_submit)
        monkeypatch.setattr(notebook, "ensure_jar", lambda: tmp_path / "stub.jar")
        monkeypatch.setattr(notebook, "ensure_auth", lambda: None)

        result = api.retry(output=str(tmp_path))

        assert captured["gee_project"] == "staged-project"
        assert captured["crs"] == "EPSG:4326"
        # 30 m at the equator constant ≈ 0.0002695 °/px.
        assert captured["pixel_size"] == pytest.approx(30.0 / 111_320.0)
        assert captured["tile_size_pixels"] == 512
        assert captured["output_tile_size_pixels"] == 1024
        assert captured["ee_expression"] == self._EXPRESSION
        assert result.next_tiles_count == 1

    def test_retry_with_mismatched_arg_raises_export_meta_mismatch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from datensee import api, notebook
        from datensee import submit as submit_mod
        from datensee.meta import ExportMetaMismatch

        self._stage_export(tmp_path)
        (tmp_path / "_failures.json").write_text(
            json.dumps(_record(error_kind="RATE_LIMITED")) + "\n"
        )

        monkeypatch.setattr(submit_mod, "submit_job", lambda *_a, **_kw: None)
        monkeypatch.setattr(notebook, "ensure_jar", lambda: tmp_path / "stub.jar")
        monkeypatch.setattr(notebook, "ensure_auth", lambda: None)

        with pytest.raises(ExportMetaMismatch) as exc_info:
            api.retry(output=str(tmp_path), output_tile_size=4096)
        assert "output_tile_size_pixels" in str(exc_info.value)

    def test_retry_without_meta_and_without_required_args_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from datensee import api, notebook
        from datensee import submit as submit_mod

        (tmp_path / "_failures.json").write_text(
            json.dumps(_record(error_kind="RATE_LIMITED")) + "\n"
        )

        monkeypatch.setattr(submit_mod, "submit_job", lambda *_a, **_kw: None)
        monkeypatch.setattr(notebook, "ensure_jar", lambda: tmp_path / "stub.jar")
        monkeypatch.setattr(notebook, "ensure_auth", lambda: None)

        with pytest.raises(FileNotFoundError) as exc_info:
            api.retry(output=str(tmp_path), project="some-project")
        assert "ee_expression" in str(exc_info.value)

    def test_retry_with_meta_and_explicit_journal_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from datensee import api, notebook
        from datensee import submit as submit_mod

        self._stage_export(tmp_path)
        custom_journal = tmp_path / "manually_curated.ndjson"
        custom_journal.write_text(json.dumps(_record(error_kind="RATE_LIMITED")) + "\n")

        monkeypatch.setattr(submit_mod, "submit_job", lambda *_a, **_kw: None)
        monkeypatch.setattr(notebook, "ensure_jar", lambda: tmp_path / "stub.jar")
        monkeypatch.setattr(notebook, "ensure_auth", lambda: None)

        result = api.retry(output=str(tmp_path), journal=custom_journal)
        assert result.next_tiles_count == 1
