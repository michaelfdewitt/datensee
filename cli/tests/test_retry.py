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

    def test_carryover_records_get_stamped_with_journal_reason(self) -> None:
        records = [
            _record(error_kind="AUTH_ERROR"),                        # terminal
            _record(error_kind="MEMORY_EXCEEDED", lineage=[0, 1]),   # depth_cap
            _record(error_kind="SOMETHING_NEW"),                     # unknown_kind
        ]
        plan = plan_retry(records)
        # Carryover order matches input order; map by error_kind for clarity.
        by_kind = {r["error_kind"]: r for r in plan.carryover}
        assert by_kind["AUTH_ERROR"]["journal_reason"] == JOURNAL_REASON_TERMINAL
        assert by_kind["MEMORY_EXCEEDED"]["journal_reason"] == JOURNAL_REASON_DEPTH_CAP
        assert by_kind["SOMETHING_NEW"]["journal_reason"] == JOURNAL_REASON_UNKNOWN_KIND

    def test_carryover_does_not_mutate_input_records(self) -> None:
        # plan_retry stamps a *copy*; the original journal records the
        # caller passed in stay untouched (important if the caller
        # re-uses them for downstream reporting).
        original = _record(error_kind="AUTH_ERROR")
        before = dict(original)  # snapshot
        plan_retry([original])
        assert original == before

    def test_allow_split_false_demotes_split_eligible_to_carryover(self) -> None:
        # In non-M6 exports (one COG per compute tile), split children
        # would clobber the parent's filename. plan_retry must refuse
        # to split when allow_split is False.
        records = [
            _record(error_kind="MEMORY_EXCEEDED"),
            _record(error_kind="COMPUTATION_TIMEOUT"),
            _record(error_kind="RATE_LIMITED"),  # retry_same is still allowed
        ]
        plan = plan_retry(records, allow_split=False)
        assert plan.stats.get("split", 0) == 0
        assert plan.stats["split_disabled"] == 2
        assert plan.stats["retry_same"] == 1
        # Two split-eligible records carry over with the right reason.
        kinds_in_carryover = {r["error_kind"] for r in plan.carryover}
        assert kinds_in_carryover == {"MEMORY_EXCEEDED", "COMPUTATION_TIMEOUT"}
        for record in plan.carryover:
            assert record["journal_reason"] == JOURNAL_REASON_SPLIT_DISABLED
        # The retry_same record produced one child, not in carryover.
        assert len(plan.next_tiles) == 1


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


# ---------------------------------------------------------------------------
# api.retry — carryover merge into _failures.json
# ---------------------------------------------------------------------------


class TestApiRetryCarryoverMerge:
    """End-to-end: api.retry() must append carryover (terminal + depth-cap)
    records back into _failures.json so the journal stays the canonical
    view of "what's still stuck" between retry rounds.
    """

    def _stub_submit(self) -> None:
        # No-op submit_job stub — pretend the pipeline ran and wrote
        # its own _failures.json with this round's new failures.
        # The merge code should append carryover on top.
        pass

    def test_terminal_kinds_appear_in_failures_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from datensee import api

        # Stage a journal with one MEMORY_EXCEEDED (will split) and one
        # AUTH_ERROR (terminal — must end up in _failures.json).
        journal = tmp_path / "_failures.json"
        terminal_record = _record(
            error_kind="AUTH_ERROR",
            bbox=(0.0, 0.0, 100.0, 100.0),
            row=0,
            col=0,
        )
        memory_record = _record(
            error_kind="MEMORY_EXCEEDED",
            bbox=(0.0, 0.0, 100.0, 100.0),
            row=0,
            col=0,
        )
        journal.write_text(
            json.dumps(memory_record) + "\n" + json.dumps(terminal_record) + "\n"
        )

        # Output dir doubles as both the input journal location and the
        # post-pipeline _failures.json location. Simulate the pipeline
        # by having the stubbed submit_job pre-write the file as the
        # real pipeline would (one new failure from this round).
        new_pipeline_failure = _record(
            error_kind="RATE_LIMITED",
            bbox=(0.0, 0.0, 50.0, 50.0),
            row=0,
            col=0,
            lineage=[0],
        )

        def fake_submit(*_args, **_kwargs):
            # Overwrite _failures.json with this round's "new" failures.
            (tmp_path / "_failures.json").write_text(
                json.dumps(new_pipeline_failure) + "\n"
            )
            return None

        # api.retry() imports submit_job, ensure_jar, ensure_auth lazily
        # from their source modules — patch there, not on the api module.
        from datensee import notebook
        from datensee import submit as submit_mod

        monkeypatch.setattr(submit_mod, "submit_job", fake_submit)
        monkeypatch.setattr(notebook, "ensure_jar", lambda: tmp_path / "stub.jar")
        monkeypatch.setattr(notebook, "ensure_auth", lambda: None)

        # output_tile_size > tile_size enables M6 two-tier mode, which is
        # required for adaptive splits to be safe — split children share
        # (row, col) with the parent, and only the M6 assembler keys
        # output COGs by bbox-derived block position rather than (row, col).
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

        # Sanity: the plan classified one record as split, one as terminal.
        assert result.stats.get("split") == 1
        assert result.stats.get("terminal") == 1
        assert result.next_tiles_count == 4  # 4 quadrant children
        assert result.carryover_count == 1   # the AUTH_ERROR

        # The journal now contains: the new pipeline failure (RATE_LIMITED)
        # plus the carried-over AUTH_ERROR. The MEMORY_EXCEEDED record is
        # NOT here directly — it was split into children that the pipeline
        # is now responsible for.
        merged_lines = (tmp_path / "_failures.json").read_text().strip().splitlines()
        merged = [json.loads(line) for line in merged_lines]
        kinds = sorted(r["error_kind"] for r in merged)
        assert kinds == ["AUTH_ERROR", "RATE_LIMITED"], (
            f"Expected the new pipeline failure plus the carried-over "
            f"AUTH_ERROR, got {kinds}"
        )
        # The carried-over AUTH_ERROR carries journal_reason="terminal"
        # so a downstream reader can tell at a glance why it's stuck.
        by_kind = {r["error_kind"]: r for r in merged}
        assert by_kind["AUTH_ERROR"]["journal_reason"] == JOURNAL_REASON_TERMINAL

    def test_depth_capped_records_appear_in_failures_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from datensee import api

        # MEMORY_EXCEEDED record already at depth 2 — won't split, must
        # end up carried over into the next _failures.json.
        capped_record = _record(error_kind="MEMORY_EXCEEDED", lineage=[0, 1])
        # Plus one progressable record so we don't short-circuit on
        # "nothing to retry".
        progressing_record = _record(error_kind="RATE_LIMITED")

        journal = tmp_path / "_failures.json"
        journal.write_text(
            json.dumps(capped_record) + "\n" + json.dumps(progressing_record) + "\n"
        )

        def fake_submit(*_args, **_kwargs):
            # Pipeline produced no new failures this round.
            (tmp_path / "_failures.json").write_text("")
            return None

        # api.retry() imports submit_job, ensure_jar, ensure_auth lazily
        # from their source modules — patch there, not on the api module.
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
        # Stamped reason captures *why* it's stuck — not the EE-side error
        # (which is MEMORY_EXCEEDED) but the retry-policy verdict.
        assert merged[0]["journal_reason"] == JOURNAL_REASON_DEPTH_CAP

    # NB: Dataflow / GCS merge path is not unit-tested — exercising it
    # requires stubbing google.cloud.storage's upload + the GCS-side
    # _failures.json layout, which isn't worth the test scaffolding for
    # what amounts to a logged warning + early return. The skip branch
    # is small enough to verify by code inspection. Tracked under the
    # "Dataflow merge isn't wired" caveat in docs/retry-with-journal.md.


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
        # Stage a journal alongside the meta — default journal location.
        record = _record(error_kind="RATE_LIMITED")
        (tmp_path / "_failures.json").write_text(json.dumps(record) + "\n")

        captured: dict[str, object] = {}

        def fake_submit(config, **_kwargs):
            captured["ee_expression"] = config.ee_expression
            captured["gee_project"] = config.gee_project
            captured["crs"] = config.tile_grid.crs
            captured["scale_meters"] = config.tile_grid.scale_meters
            captured["tile_size_pixels"] = config.tile_grid.tile_size_pixels
            captured["output_tile_size_pixels"] = config.output.output_tile_size_pixels
            (tmp_path / "_failures.json").write_text("")
            return None

        monkeypatch.setattr(submit_mod, "submit_job", fake_submit)
        monkeypatch.setattr(notebook, "ensure_jar", lambda: tmp_path / "stub.jar")
        monkeypatch.setattr(notebook, "ensure_auth", lambda: None)

        result = api.retry(output=str(tmp_path))

        # Everything should have been resolved from the staged meta.
        assert captured["gee_project"] == "staged-project"
        assert captured["crs"] == "EPSG:4326"
        assert captured["scale_meters"] == 30.0
        assert captured["tile_size_pixels"] == 512
        assert captured["output_tile_size_pixels"] == 1024
        assert captured["ee_expression"] == self._EXPRESSION
        assert result.next_tiles_count == 1  # the RATE_LIMITED retry-same

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

        # The original output_tile_size was 1024; passing a different
        # value must raise rather than silently corrupt the output grid.
        with pytest.raises(ExportMetaMismatch) as exc_info:
            api.retry(output=str(tmp_path), output_tile_size=4096)
        assert "output_tile_size_pixels" in str(exc_info.value)

    def test_retry_without_meta_and_without_required_args_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from datensee import api, notebook
        from datensee import submit as submit_mod

        # No meta sidecar.
        (tmp_path / "_failures.json").write_text(
            json.dumps(_record(error_kind="RATE_LIMITED")) + "\n"
        )

        monkeypatch.setattr(submit_mod, "submit_job", lambda *_a, **_kw: None)
        monkeypatch.setattr(notebook, "ensure_jar", lambda: tmp_path / "stub.jar")
        monkeypatch.setattr(notebook, "ensure_auth", lambda: None)

        # Legacy export with no meta and no expression supplied → must
        # raise FileNotFoundError naming what's missing.
        with pytest.raises(FileNotFoundError) as exc_info:
            api.retry(output=str(tmp_path), project="some-project")
        assert "ee_expression" in str(exc_info.value)

    def test_retry_with_meta_and_explicit_journal_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The journal arg is independent of the meta path — caller can
        point retry at a specific journal even when meta is alongside."""
        from datensee import api, notebook
        from datensee import submit as submit_mod

        self._stage_export(tmp_path)
        custom_journal = tmp_path / "manually_curated.ndjson"
        custom_journal.write_text(
            json.dumps(_record(error_kind="RATE_LIMITED")) + "\n"
        )

        monkeypatch.setattr(submit_mod, "submit_job", lambda *_a, **_kw: None)
        monkeypatch.setattr(notebook, "ensure_jar", lambda: tmp_path / "stub.jar")
        monkeypatch.setattr(notebook, "ensure_auth", lambda: None)

        result = api.retry(output=str(tmp_path), journal=custom_journal)
        assert result.next_tiles_count == 1
