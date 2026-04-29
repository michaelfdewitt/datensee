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
  "last_seen": "2026-04-29T13:02:11Z"
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

Today (this commit), only the **wire contract** is in place:

- `TileCoordinate.lineage` (default empty) — Python + Java.
- `FailedTileRecord` Java record with all the journal fields.
- `EeErrorKind` enum with the full set of kinds.
- `FailedTileWriter` emits the new format with `error_kind=UNKNOWN` and timestamps. The classifier isn't wired up — the dead-letter side output upstream still emits raw `TileCoordinate` and the kind can't be inferred without changing that signal.
- `TileCoordinate` ignores unknown JSON fields, so any journal record is valid `tiles_file` input today.

Follow-ups:
1. Plumb the EE response classifier through `TileFetchDoFn`'s dead-letter side output (typed as `FailedTileRecord`, not `TileCoordinate`).
2. Implement the `datensee retry` CLI command.
3. Implement the splitter (with depth cap + opt-in flag).
4. Add a regression test using a synthesized "memory limit exceeded" response that gets split correctly through one round.
