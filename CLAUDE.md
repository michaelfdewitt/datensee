# CLAUDE.md — DatensEE Orchestrator

> See also: [`docs/handoff.md`](docs/handoff.md) for deployment story, COG pipeline gotchas, recent fixes, known limitations, and the test runbook.

## What This Project Is

A CLI + Python library that lets Google Earth Engine users run image exports at massive scale by parallelizing tile fetches across Google Cloud Dataflow workers. Users provide the same computation description they already use in Earth Engine — the tool handles tiling, parallel fetching via the EE High Volume API, assembly into Cloud Optimized GeoTIFFs, and upload to GCS. Works from the terminal, Python scripts, or Colab/Jupyter notebooks.

**The key insight:** We don't need to understand or recompile EE computations. Earth Engine evaluates its own expression graph per-tile — we just need to call it a lot, in parallel, and stitch the results together. This is a massively parallel tile fetcher with smart orchestration, not a computation framework.

## Architecture Overview

```
                          ┌──────────────────────────────┐
                          │     Earth Engine Backend     │
                          │  (evaluates computation      │
                          │   per-tile via HV endpoint)  │
                          └──────────▲───────────────────┘
                                     │ High Volume API
                                     │ (thousands of concurrent tile fetches)
                                     │
┌──────────────┐          ┌──────────┴───────────────────┐          ┌────────────┐
│  Python CLI  │─────────▶│     Dataflow / Local Runner   │─────────▶│  GCS       │
│              │ submits   │                               │ writes   │  (COG)     │
│  • Parse EE  │ job      │  Create tile coords           │ output   │            │
│    params    │          │  → ParDo: fetch tile (HV API) │          │  or GDAL   │
│  • Tile the  │          │  → Assemble raster            │          │  → other   │
│    region    │          │  → Write COG to GCS           │          │    formats │
│  • Submit    │          │                               │          │            │
└──────────────┘          └───────────────────────────────┘          └────────────┘
```

### How It Works

1. **User provides:** An EE computation description (serialized expression + region + scale/CRS + output destination)
2. **Python CLI does:**
   - Validates inputs
   - Decomposes the region into a tile grid at the requested scale/projection
   - Submits a Dataflow job (or runs locally for small regions)
3. **Java Beam pipeline does:**
   - Receives tile coordinates as input PCollection
   - Each worker fetches its tiles via EE High Volume API (parallel, rate-limited, with retries)
   - Transcodes each tile to COG (internal tiling + deflate compression) via pure-Java TIFF rewriter
   - Writes COG tiles to GCS or local filesystem (one COG per output tile; M6 two-tier mode controls granularity via `output_tile_size_pixels`)
4. **Post-processing (optional):** none required — output COGs are self-describing GeoTIFFs that any modern GIS tool reads directly. No VRT, no XML manifest.

### Python Side (`/cli`)
- **Framework:** Typer (CLI) + Pydantic (config validation)
- **Package manager:** uv
- **Responsibility:** UX, tiling strategy, job submission, status monitoring
- **Key logic:** Region → tile grid decomposition, respecting projection and scale
- **Tests:** pytest

### Java Side (`/pipelines`)
- **Build:** Gradle (Kotlin DSL)
- **Framework:** Apache Beam SDK (Dataflow + Direct runners)
- **Responsibility:** Distributed tile fetching, raster assembly, GCS output
- **Key logic:** HV API client (auth, rate limiting, retries, backoff), tile-to-COG assembly
- **Tests:** JUnit 5 + Beam TestPipeline

### Python API (`api.py` + `notebook.py`)

The public Python API lives in `api.py` — the CLI (`main.py`) is a thin wrapper. Key functions:

- `datensee.export(ee_expression, region, project, output, ...)` → `ExportResult` — full export pipeline
- `datensee.demo(project, output)` → `ExportResult` — built-in NDVI demo
- `datensee.tile(region, scale, crs, tile_size)` → `TileGrid` — region decomposition only
- `datensee.poll(job_id, project, region, callback=...)` → `JobState` — Dataflow job polling

`notebook.py` provides Colab/Jupyter adapters:

- `notebook.ensure_auth()` — triggers `google.colab.auth` when ADC unavailable
- `notebook.ensure_jar()` — auto-downloads JAR if not found locally
- `notebook.display_job_progress(job_id, ...)` — HTML polling display
- `notebook.display_export_summary(config)` — HTML config summary
- `notebook.display_tile_grid(grid, region)` — matplotlib tile map
- `notebook.preview_tiles(output, config, n=4)` — matplotlib tile images

`submit.py` and `status.py` accept optional callbacks (`progress_callback`, `status_callback`) so the notebook layer can replace Rich with HTML rendering without touching business logic.

### The Contract (`/contract`)
The pipeline config passed from Python → Java. Defines:
- EE computation description (opaque serialized expression — we don't interpret this)
- Tile grid: list of tile coordinates + CRS + scale
- Output: GCS path, COG parameters
- Runner config: Dataflow project/region/worker settings OR local mode flag

## Repository Structure

```
datensee/
├── CLAUDE.md
├── LICENSE
├── cli/
│   ├── pyproject.toml
│   ├── src/
│   │   └── datensee/
│   │       ├── __init__.py      ← Re-exports public API (export, demo, poll, tile)
│   │       ├── api.py           ← Public Python API — orchestration logic
│   │       ├── notebook.py      ← Colab/Jupyter detection, auth, HTML displays
│   │       ├── main.py          ← Typer CLI (thin wrapper around api.py)
│   │       ├── config.py        ← Pydantic models for pipeline config + tile_count/raw_output_bytes
│   │       ├── tiling.py        ← Region → tile grid decomposition
│   │       ├── submit.py        ← Dataflow job submission + local progress
│   │       ├── status.py        ← Job polling with Dataflow metrics
│   │       ├── display.py       ← Rich panels (export summary, post-run)
│   │       ├── jar.py           ← Pipeline JAR discovery, download, build
│   │       └── data/            ← Bundled JSON (demo expression + region)
│   └── tests/
├── pipelines/
│   ├── build.gradle.kts
│   ├── src/main/java/
│   │   └── com/datensee/
│   │       ├── DatensEEPipeline.java  ← Beam pipeline definition
│   │       ├── options/               ← PipelineOptions
│   │       ├── fetch/                 ← HV API client, rate limiter, retry logic
│   │       ├── assemble/              ← Tile → raster assembly
│   │       └── io/                    ← COG writer, GCS sink
│   └── src/test/java/
├── contract/
│   ├── pipeline-config.schema.json
│   └── examples/
│       └── ndvi-california.json
├── notebooks/
│   └── datensee_quickstart.ipynb  ← End-to-end Colab example
└── docs/
```

## Coding Conventions

Generally, you should aim for clean, functional (as in lambdas) code. This
should look professional and a little autistic (in a nice way!).

### Python
- Python 3.12+ (use modern syntax: `X | Y` unions, `match` statements)
- Type hints on every function signature — no exceptions
- Pydantic v2 for all data models (use `model_validator`, not legacy)
- Typer for CLI with explicit `typer.Option()` / `typer.Argument()` annotations
- Format with `ruff format`, lint with `ruff check`
- Tests: pytest with fixtures, no unittest-style classes
- Prefer `pathlib.Path` over `os.path`
- Use `httpx` for HTTP calls (async-capable), not `requests`
- Docstrings: Google style

### Java
- Java 25 (LTS) — use records, sealed interfaces, pattern matching
- Gradle Kotlin DSL (`build.gradle.kts`)
- Beam SDK conventions: PTransforms are top-level classes, not lambdas
- Use records for data types (tile coordinates, fetch results, config)
- Tests: JUnit 5 + `TestPipeline` from Beam testing utilities
- Google Java Style (enforced via Checkstyle)
- Prefer immutable data structures

### Both
- Commit messages: Conventional Commits (`feat:`, `fix:`, `chore:`, etc.)
- No abbreviations in public APIs
- Error messages must be actionable: say what went wrong AND what to do

## Key Dependencies

### Python
- `typer[all]` — CLI framework
- `pydantic>=2.0` — config validation
- `httpx` — HTTP client for Dataflow API and EE auth
- `rich` — terminal output, progress bars, log streaming
- `jsonschema` — contract validation
- `pyproj` — CRS handling for tiling logic
- `shapely` — geometry operations for region decomposition

### Java
- `org.apache.beam:beam-sdks-java-core`
- `org.apache.beam:beam-runners-google-cloud-dataflow-java`
- `org.apache.beam:beam-runners-direct-java` — local runner for testing/small jobs
- `com.google.cloud:google-cloud-storage` — GCS output
- `com.google.auth:google-auth-library-oauth2-http` — EE + GCP auth
- `com.fasterxml.jackson.core:jackson-databind` — config parsing
- `org.gdal:gdal` — COG assembly (or pure Java alternative TBD)

## Hard Technical Problems

These are the areas where the real complexity lives:

1. **Rate limiting the HV API.** The High Volume endpoint has per-project quotas. Workers need coordinated rate limiting — not just per-worker backoff, but global awareness of the request budget. Options: shared counter via Beam state, or a token bucket backed by Memorystore/Redis.

2. **Tiling strategy.** Naive regular grids waste requests in sparse regions and can hit memory limits in dense ones. Adaptive tiling (quadtree decomposition based on data density or complexity) is a future optimization but adds significant complexity. Start with regular grids.

3. **Raster assembly at scale.** Stitching thousands of tiles into a single COG that's terabytes in size is non-trivial. The current shape: M6 two-tier tiling (`output_tile_size_pixels`) lets users dial output granularity from "one COG per fetch" up to "one COG per region" without ever generating a manifest file. No VRT, no XML — output COGs are self-describing.

4. **Auth propagation.** EE auth tokens need to reach every Dataflow worker. Beam's credential propagation works for GCP services but EE auth is separate. May need to pass refresh tokens via pipeline options or use workload identity federation.

5. **Error handling at scale.** When 50 out of 10,000 tile fetches fail, the right behavior is retry → skip → report, not abort the whole job. Beam's built-in retry helps but the UX of partial failures needs design.

## Design Principles

1. **EE is the computation engine. We are the parallelism engine.** Never interpret or optimize the EE expression — but do compose with it (e.g. wrapping in `Image.clip(region)` for edge tiles).
2. **The API is the product, the CLI is one surface.** `api.py` owns orchestration; `main.py` (CLI) and `notebook.py` (Colab/Jupyter) are thin display layers. Every rough edge is a user lost.
3. **Fail fast, fail loud.** Validate everything in Python before submitting the Dataflow job.
4. **EE users aren't infra engineers.** Abstract away Dataflow concepts behind opinionated defaults with escape hatches.
5. **COG to GCS is the primitive.** All other formats are GDAL post-processing.
6. **Precision is everything.** You'll need to make sure that everything aligns exactly. Don't accept pixel misaligment or projection mismatches.

## Current Status

🟢 **Notebook Integration** — Public Python API (`datensee.export()`, `tile()`, `poll()`), Colab auto-auth, HTML display adapters, quickstart notebook. All milestones M1–M5 + notebook integration complete.

## Milestones

1. **M1: Proof of Life** ✅ — CLI takes a hardcoded NDVI expression + small region, tiles it, submits to Dataflow, fetches tiles via HV API, writes a single GeoTIFF to GCS.
2. **M2: Real Config** ✅ — Pipeline config schema defined. CLI accepts arbitrary EE expressions, regions, scales. Local runner works for small jobs.
3. **M3: Scale** ✅ — Partial failure tolerance, per-worker rate limiting, smart retry classification, file-based tile input.
4. **M4: UX Polish** ✅ — Rich progress bar (local mode), cost estimation (EECU range, Dataflow USD, storage), summary panels, confirmation for large jobs, enhanced Dataflow status polling with metrics.
5. **M5: Distribution** ✅ — `pip install datensee`, smart JAR discovery, `datensee jar` subcommands (download/build/path), Apache 2.0 license, full PyPI metadata.
6. **Notebook Integration** ✅ — Public Python API (`api.py`), Colab/Jupyter auto-auth, HTML display adapters (job progress, cost estimate, tile grid, tile preview), quickstart notebook, `notebook`/`all` optional dependency groups.
7. **M6: Two-Tier Tiling** ✅ — Compute tiles (small, for EE HV API) and output tiles (large, for practical file counts) are separated. Compute tiles are fetched in parallel, then grouped by output tile via Beam `GroupByKey`, assembled by `AssembledCogWriter` into a contiguous pixel buffer, and written as multi-block COGs whose internal block size equals the compute tile size. Opt-in via `output.output_tile_size_pixels` (must be a multiple of `tile_grid.tile_size_pixels`); the default behavior is one COG per compute tile. See [`docs/handoff.md`](docs/handoff.md) for the wiring summary.
8. **Adaptive Retry (Quadtree)** ✅ — `datensee retry --journal _failures.json` reads a structured failures journal, classifies each entry by `error_kind` (populated by `EeErrorKind.classify` against the EE HV response), and either splits the tile into 4 quadrant children (for `MEMORY_EXCEEDED` / `COMPUTATION_TIMEOUT`) or retries the same bbox (for transient infra). Default max depth 2; conservative split allowlist (only EE complexity signals). See [`docs/retry-with-journal.md`](docs/retry-with-journal.md).
9. **Pipeline-Side Carryover Merge** — Move the failures-journal carryover merge into the Java pipeline so it works on Dataflow as well as local. Today `api.retry()` appends terminal + depth-capped records onto `_failures.json` after `submit_job` returns, which works in local mode but races the async writer on Dataflow. The fix: retry CLI writes `{output}/_carryover.json` alongside `_retry_tiles.json`; the Java side reads that file via a small `CarryoverReadTransform`, unions it with the failed PCollection downstream of fetch, and `TextIO.write` emits the combined `_failures.json`. Same code path everywhere, no Python-side post-step, no race. Removes the "Dataflow merge isn't wired" caveat from `docs/handoff.md`.
10. **M10: Snapshot Pinning** ✅ — Every export captures a single Unix-microseconds timestamp `T` at submit and rewrites every asset-load node in the serialized EE expression to pin its `version` arg to `T`. All workers see one snapshot, so a mutating ImageCollection can't let tile A see the new version while tile B sees the old one. `T` is stored in `_export_meta.json` and inherited by `datensee retry` so split children land on the same snapshot as their parents. BigQuery loads can't be pinned through this mechanism: `runBigQuery` is rejected unless its SQL contains `FOR SYSTEM_TIME AS OF`, and `loadBigQuery` is unconditionally rejected. Walker lives in [`cli/src/datensee/pinning.py`](cli/src/datensee/pinning.py); `--snapshot-time` (ISO-8601 or Unix microseconds) is the user-facing override. **Units note:** EE's `version` load argument is a Long compared against the asset's `system:version` field, which is a microsecond timestamp — passing nanoseconds lands in a value range (`> ~1e17`) where EE's version comparison crashes with `gRPC INTERNAL`, surfaced to callers as silent client-side hangs. `pin_expression` rejects any value above `1e17`; legacy nano values in `_export_meta.json` are migrated in-place at read time with a warning.
11. **M11: PixelGrid as canonical export shape** ✅ — Every export normalizes to a single `PixelGrid` (`{crs_code, affine_transform, dimensions}`) that mirrors EE's own type, sent verbatim to `computePixels`. Tiles inside the export are integer pixel rectangles (`col_px`, `row_px`, `width_px`, `height_px`) within that parent grid; float bboxes are derived from `transform × pixel_offsets` and never persisted. `scale_meters` for geographic CRSs uses the equator constant (no latitude correction), so cross-export grid alignment is unconditional — same CRS + same scale + same tile size = identical pixel-to-CRS transform regardless of region. The parent grid is persisted in `_export_meta.json` so retry can reconstruct CRS coordinates from journal records' local pixel offsets. Closes the geographic-CRS footgun M10 introduced; `AssembledCogWriter`'s float tolerance + off-grid/off-block guards collapse to integer arithmetic by construction. See [`docs/m11-pixel-grid.md`](docs/m11-pixel-grid.md) for the design.

## Testing

### Integration tests (EE HV API)

GCP project for testing: **`datensee-testing`** (registered for non-commercial EE use).

Run integration tests:
```bash
cd cli && uv run pytest tests/test_integration_ee.py --integration --gee-project=datensee-testing -v
```

Tests use SRTM DEM (`USGS/SRTMGL1_003`) — pre-cached and cheap but spatially varying,
so tiling and alignment bugs are caught that constant images would miss.

EECU usage is tracked automatically: `conftest.py` records wall time at session start
and queries Cloud Monitoring (`earthengine.googleapis.com/project/cpu/usage_time`) at
session end, printing a usage report. Baseline for a full 21-test run: **~64 EECU-seconds
(~1 EECU-minute, ~0.018 EECU-hours)**. If this number grows significantly, investigate
which tests became more expensive.

Unit tests (no network):
```bash
cd cli && uv run pytest -v
```

## Notes for Claude

- When generating code, always include type hints (Python) or type annotations (Java). No untyped code.
- Prefer small, composable functions/methods over large monolithic ones.
- When in doubt about a Beam API, check https://beam.apache.org/documentation/ — don't guess.
- When in doubt about the EE HV API, check https://developers.google.com/earth-engine/reference — don't guess.
- The user is a staff engineer. Skip boilerplate explanations; be direct and precise.
- If a design decision has tradeoffs, name them explicitly rather than picking silently.
- The EE computation is opaque. Never parse, optimize, or interpret the expression graph — but composing with it (wrapping in clip, cast, etc.) is fine.