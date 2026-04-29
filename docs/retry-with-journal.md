# Retry-with-Journal — Adaptive Quadtree Retry

> **Status: contract sketched, implementation pending.** This document defines the wire format and semantics so the format is stable from day one. The actual `datensee retry` command and quadtree-splitting logic are follow-up work.

## The idea

When a tile fetch fails, the failure journal (`{output}/_failures.json`) is a complete description of what to try again. A new command — call it `datensee retry --journal failures.json` — feeds the journal back into the same pipeline. For each failed entry it either:

1. **Retries the tile as-is** (transient errors: rate-limit, generic 5xx, timeout-but-not-EE-timeout).
2. **Splits the tile into 4 quadrants** and submits them as new compute tiles (EE-specific complexity errors: `MEMORY_EXCEEDED`, `COMPUTATION_TIMEOUT`).

Successful sub-tiles flow into the existing M6 assembler keyed by `(out_row, out_col)`, so split children land in the same output COG as their parent — at smaller pixel granularity but at correct positions, because the bounding box is the geometric truth.

The split is recursive but bounded: each round emits a fresh `failures.json` with whatever is still failing; the operator (or an outer driver) re-runs `datensee retry` until either the journal is empty or every remaining failure has hit the depth cap.

## Why it's an adaptive primitive

EE workloads are heterogeneous. A continental-scale Sentinel-2 NDVI export will have, say, 9,997 tiles that succeed at 256×256 px with the default fetch budget, plus 3 tiles whose underlying expression hits the per-tile memory limit (typically because of dense filtering or many concurrent reducers in those geographic patches). The right answer for those 3 tiles is to fetch them at a smaller area-per-call and assemble — not to abort the whole job, not to globally lower the tile size, and not to silently skip them.

This is genuinely differentiating. The built-in `Export.image.toCloudStorage` doesn't have a per-tile failure model at all.

## Wire format

`{output}/_failures.json` is NDJSON, one record per line. Each record matches `FailedTileRecord` (Java) and the equivalent Pydantic model on the Python side. The format is a **superset of `TileCoordinate`**: extra fields are tolerated by `TileCoordinateParser` (Java: `@JsonIgnoreProperties(ignoreUnknown=true)`) so the journal is directly consumable as a `tiles_file` input.

```json
{
  "x_min": -120.5, "y_min": 37.5, "x_max": -120.4, "y_max": 37.6,
  "row": 3, "col": 5,
  "out_row": 0, "out_col": 0,
  "lineage": [],
  "error_kind": "MEMORY_EXCEEDED",
  "error_message": "User memory limit exceeded.",
  "http_status": 400,
  "attempts": 4,
  "first_seen": "2026-04-29T13:00:00Z",
  "last_seen": "2026-04-29T13:02:11Z",
  "journal_reason": "depth_cap"
}
```

### Field semantics

| Field | Type | Notes |
|---|---|---|
| `x_min`, `y_min`, `x_max`, `y_max` | float | Bounding box in target CRS. Geometric truth — drives both fetch and assembly. |
| `row`, `col` | int | **Root** compute tile indices (within the export bbox). Pinned across splits — see "Lineage" below. |
| `out_row`, `out_col` | int | M6 output-tile indices. Split children inherit these from their parent. |
| `lineage` | list[int] | Quadtree path from root tile. See below. |
| `error_kind` | enum | Classification used by the retry decision. See `EeErrorKind`. |
| `error_message` | string | Truncated EE response body for debugging. |
| `http_status` | int? | HTTP status (when applicable). |
| `attempts` | int | Total fetch attempts across journal cycles, for depth budgeting. |
| `first_seen` / `last_seen` | ISO8601 | Useful when triaging journals manually. |
| `journal_reason` | string | Latest retry-policy verdict — *why* the record is in the journal. See below. |

### `journal_reason` — why the record is sitting in the journal

`error_kind` says what EE told us went wrong. `journal_reason` says what the system decided to do (or not do) about it the last time the journal was touched. The two are distinct:

| `journal_reason` | When written | Meaning |
|---|---|---|
| `"failed"` | Pipeline-emitted dead-letter. Always the value out of `TileFetchDoFn`. | Fresh failure, retry-eligible. The next `datensee retry` run will try to do something with this record. |
| `"depth_cap"` | Re-stamped by `datensee retry` when an entry was split-eligible (`MEMORY_EXCEEDED` / `COMPUTATION_TIMEOUT`) but `len(lineage) >= max_depth`. | Stuck under the current depth budget. Bumping `--max-depth` rescues these. |
| `"terminal"` | Re-stamped by `datensee retry` when `error_kind` is in the terminal set (`AUTH_ERROR`, `FATAL_REQUEST`). | Stuck permanently — the retry policy never touches these regardless of config. |
| `"unknown_kind"` | Re-stamped by `datensee retry` when `error_kind` isn't recognized by the current policy (e.g. EE adds a new error type). | Held in the journal deliberately so a new failure mode never silently disappears. |

The field is **mutable across rounds**: a tile that's `"failed"` after the first export may become `"depth_cap"` after a few retry rounds once it's exhausted the split budget. The journal reflects the latest verdict, not history. A user can `jq '.[] | select(.journal_reason != "failed")'` to find tiles that won't make further progress without manual intervention.

The canonical strings are constants on both sides:
- Java: `FailedTileRecord.JOURNAL_REASON_FAILED` etc.
- Python: `datensee.retry.JOURNAL_REASON_FAILED` etc.

Schema is backward-compatible — older journals without the field will deserialize cleanly (Jackson's `@JsonIgnoreProperties(ignoreUnknown=true)` on `TileCoordinate` covers this; consumers that read `FailedTileRecord` should treat missing values as `"failed"`).

### Lineage — quadtree path representation

`lineage` is a list of integers `0–3` describing the path from the root compute tile to the current sub-tile, one entry per split level. Empty list = root tile (the common case).

The mapping is **CRS-axis-order-independent**:

```
  ┌─────┬─────┐                         ┌─────┬─────┐
  │  2  │  3  │   y-high (north in     │  0  │  1  │   y-low side  
  ├─────┼─────┤   most projections)    ├─────┼─────┤
  │  0  │  1  │   y-low                │  2  │  3  │   y-high
  └─────┴─────┘                         └─────┴─────┘
```

`0=x_low/y_low, 1=x_high/y_low, 2=x_low/y_high, 3=x_high/y_high`. Whether "y-high" is north or south depends on the CRS axis order; the quadrant index is defined in terms of bbox coordinates, not compass directions. **Don't introduce `NW`/`NE`/etc. anywhere** — that breaks for projections where the y-axis is flipped.

### Why `(row, col)` are pinned to the root

Two failure rounds would conflict if `(row, col)` rolled forward with each split — you'd get sub-tiles with the same `(row, col)` as their grandchildren and you couldn't tell them apart. Pinning `(row, col)` to the root and using `lineage` to disambiguate means:

- A tile is uniquely identified by `(out_row, out_col, row, col, lineage)`.
- The bbox is the canonical retry input — two different lineage paths yielding the same bbox are equivalent (and an idempotent dedupe pass could collapse them).
- The assembler doesn't need to know about lineage at all — it keys on `(out_row, out_col)` and writes pixels at offsets implied by the bbox.

## Error classification

The classifier inspects HTTP status and (for HTTP 400) the EE response body:

| `error_kind` | Trigger | Retry policy |
|---|---|---|
| `MEMORY_EXCEEDED` | HTTP 400 + body matches `/memory limit exceeded/i` | **Split** |
| `COMPUTATION_TIMEOUT` | HTTP 400 + body matches `/timed out/i` | **Split** |
| `RATE_LIMITED` | HTTP 429 | Retry same tile, exponential backoff |
| `RETRYABLE_SERVER` | HTTP 5xx | Retry same tile, exponential backoff |
| `AUTH_ERROR` | HTTP 401 / 403 with auth-error body | **Don't retry** — surface to user |
| `FATAL_REQUEST` | HTTP 400 / 403 / 404 (no EE-specific signature) | **Don't retry** |
| `UNKNOWN` | Anything else | Retry same tile, log full body |

The split allowlist is **intentionally minimal**. Splitting on a generic 5xx would mask transient infrastructure issues; splitting on auth would yield 4 more auth failures. Adding a kind to the split allowlist is a one-line config change later — removing one that's already triggering production cascades is a fire.

EE doesn't expose a structured error code for OOM/timeout. The discriminant is the body string, which is documented under EE's [debugging guide](https://developers.google.com/earth-engine/guides/debugging). The classifier should match case-insensitively, on stable substrings only, and fall back to `UNKNOWN` when the body doesn't match any known pattern (so we don't silently mis-classify a future EE error message change).

## Retry semantics

A round of `datensee retry --journal failures.json` is, conceptually:

```python
for record in journal:
    if record.error_kind in SPLIT_ALLOWLIST and len(record.lineage) < MAX_DEPTH:
        for q in [0, 1, 2, 3]:
            yield split_tile(record, quadrant=q)  # bbox halved, lineage extended
    elif record.error_kind in RETRY_ALLOWLIST:
        yield TileCoordinate.from_record(record)   # same bbox, same lineage
    else:  # FATAL_REQUEST, AUTH_ERROR
        yield record  # write straight back to next round's failures journal
```

Each round produces a new failures journal. The driver loops until either the journal is empty or the journal contains only entries that won't make progress (everything is at depth cap or in a non-retry kind).

### Defaults

- `MAX_DEPTH` = **2** (1 → 4 → 16 sub-tiles per root compute tile, max).
- `SPLIT_ALLOWLIST` = `{MEMORY_EXCEEDED, COMPUTATION_TIMEOUT}`.
- `RETRY_ALLOWLIST` = `{RATE_LIMITED, RETRYABLE_SERVER, UNKNOWN}`.
- Adaptive splitting is **opt-in** via a config flag (default off). A first-time user gets dead-letter behavior, not surprise quadtree cascades.

## What's actually in the tree right now

The full path is implemented:

- **Wire contract** — `TileCoordinate.lineage` (Python + Java), `FailedTileRecord` (Java record covering all journal fields), `EeErrorKind` enum with the full set of kinds, `TileCoordinate` Jackson-tolerant of unknown fields so journals feed back as `tiles_file`.
- **Classifier** — `EeErrorKind.classify(httpStatus, body)` matches "memory limit"/"timed out" substrings against (status, body), with stable fallbacks for unmatched cases. `TileFetchDoFn.classifyFailure` walks the cause chain to find the original `EeApiException` and stamps the dead-letter record with the proper kind, status, and truncated body.
- **Dead-letter side output** — `TileFetchDoFn.FAILED_TAG` is typed `FailedTileRecord`. `FailedTileWriter` serializes it directly. `_failures.json` now carries real `error_kind` values, not placeholders.
- **Python splitter** — `datensee.retry.decide(record, max_depth, allowlists)` returns one of `split` (4 quadrant children), `retry_same` (1 child, same bbox), `depth_cap` (no children, carried over), `terminal` (no children), `unknown_kind` (no children). `split_tile(parent)` does pure-geometry quadrant bisection in bbox coordinates (axis-order-independent). `plan_retry(records)` aggregates a stream into a `RetryPlan` with `next_tiles`, `carryover`, and per-action `stats`.
- **`datensee retry` CLI** — reads a journal, runs `plan_retry`, writes the next-round tiles to `{output}/_retry_tiles.json` (or stages to GCS), and submits a fresh pipeline run with `tile_grid.tiles_file` set. Same export-style flags so the user keeps full control of pipeline parameters; `--max-depth` defaults to 2, capped at 6.

### Tests pinned

- 11 classifier tests (`EeErrorKindTest`) — every (status, body) → kind decision, including case-insensitive matching and the conservative split allowlist.
- 2 `classifyFailure` tests in `TileFetchDoFnTest` — unwraps `EeApiException` from a wrapped IOException, falls back to `UNKNOWN` for non-EE exceptions.
- 21 Python tests in `test_retry.py` — `split_tile` quadrant geometry, `decide` branches for every kind, depth cap behavior, custom allowlist override, journal I/O round-trip, and a mixed-stream `plan_retry` test.

### `_failures.json` is the canonical view of stuck tiles

After each retry round, `api.retry` appends carryover records (terminal kinds + depth-capped split-eligible records) into the new `_failures.json` that the pipeline just wrote. The journal is therefore always the complete current view: new failures from this round + everything from previous rounds that's still stuck. Re-running `datensee retry --journal _failures.json` against it is idempotent — terminal records get re-classified as terminal next round, land back in carryover, get re-merged. No accumulation past steady state.

This means:
- An empty `_failures.json` is the unambiguous "everything succeeded" signal.
- Bumping `--max-depth` on a later retry round can rescue previously-capped tiles, because they're still in the journal.
- A user looking at `_failures.json` sees auth errors and depth-capped tiles, not just whatever happened to fail in the most recent pipeline run.

### Known limitations

- **No automatic retry loop yet.** The user re-runs `datensee retry` themselves until the journal is empty. A wrapper that loops with backoff is a small follow-up but would change the UX surface, so it's left as a separate task.
- **Dataflow / GCS carryover merge isn't wired yet.** `_failures.json` lives in GCS for Dataflow runs, and the pipeline writes it asynchronously (after `submit_job` returns), so the Python-side append-after-submit approach used in local mode would race the pipeline writer. Today the carryover is logged and dropped between rounds in Dataflow; local-mode retries are the supported path.

  **Planned approach: move the merge into the Java pipeline.** The retry CLI will write the carryover as an additional input file (e.g. `{output}/_carryover.json`); the pipeline's failures-journal write will read that file at runtime and union it with this round's new failures before writing `_failures.json`. This keeps the journal as the single source of truth, avoids the post-step / race / orchestration complexity of a separate `retry-finalize`, and works identically across local + Dataflow. See `docs/handoff.md` and the milestone list in `CLAUDE.md`.
- **Retry assumes pipeline-config parity with the original export.** Children inherit `(out_row, out_col)` from their parents, so they need to land in the same M6 output COG; the assembler relies on the same `output_tile_size_pixels` setting. The CLI takes the same flags as `export`; the user is responsible for keeping them aligned.
