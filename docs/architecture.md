# DatensEE — Architecture & Conventions

High-level architecture, core design decisions, and coding conventions for DatensEE.

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
   - Each worker fetches its tiles via EE High Volume API (parallel, with retries and 429-driven exponential backoff — no client-side QPS limiter; EE's quota system is the rate-shaping signal)
   - Transcodes each tile to COG (internal tiling + deflate compression) via pure-Java TIFF rewriter
   - Writes COG tiles to GCS or local filesystem (one COG per output tile; two-tier mode controls granularity via `output_tile_size_pixels`)
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
- **Key logic:** HV API client (auth, retries, 429-driven exponential backoff), tile-to-COG assembly
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

`submit.py` and `status.py` accept optional callbacks (`progress_callback`, `status_callback`) so the notebook layer can replace Rich with HTML rendering without touching business logic.

### The Contract (`/contract`)
The pipeline config passed from Python → Java. Defines:
- EE computation description (opaque serialized expression — we don't interpret this)
- Tile grid: list of tile coordinates + CRS + scale
- Output: GCS path, COG parameters
- Runner config: Dataflow project/region/worker settings OR local mode flag

## Repository Structure

The shape splits cleanly between *shared infrastructure* (auth, snapshot pinning, Dataflow submission, job polling, the runner-agnostic envelope of `PipelineConfig`) and a *pixel pipeline subpackage* (`datensee.pixel` / `com.datensee.pixel`) that owns the raster-specific machinery — tile decomposition, the EE `computePixels` fetcher, COG output, and the raster-shaped Pydantic / record models. The seam exists so a future vector pipeline (EE `computeFeatures` → reduceRegions → GeoParquet) can land as a sibling subpackage without disturbing the pixel surface. See "The Pixel/Vector Seam" below for the wire-format contract.

```
datensee/
├── LICENSE
├── cli/
│   ├── pyproject.toml
│   ├── src/
│   │   └── datensee/
│   │       ├── __init__.py      ← Re-exports public API (export, demo, poll, tile)
│   │       ├── api.py           ← Public Python API — orchestration logic
│   │       ├── notebook.py      ← Colab/Jupyter detection, auth, HTML displays
│   │       ├── main.py          ← Typer CLI (thin wrapper around api.py)
│   │       ├── config.py        ← PipelineConfig envelope (kind discriminator,
│   │       │                       ee_expression, runner, snapshot, etc.)
│   │       ├── submit.py        ← Dataflow job submission + local progress
│   │       ├── status.py        ← Job polling with Dataflow metrics
│   │       ├── display.py       ← Rich panels (export summary, post-run)
│   │       ├── meta.py          ← _export_meta.json sidecar (used by retry)
│   │       ├── pinning.py       ← EE snapshot-time pinning (microseconds)
│   │       ├── jar.py           ← Pipeline JAR discovery, download, build
│   │       ├── data/            ← Bundled JSON (demo expression + region)
│   │       └── pixel/           ← Pixel-pipeline subpackage
│   │           ├── config.py    ← PixelGrid, TileGrid, OutputConfig, PixelPayload
│   │           ├── tiling.py    ← Region → tile grid decomposition
│   │           ├── retry.py     ← Quadtree splitter, journal I/O, decide()
│   │           └── validation/  ← Output-shape integrity checks (E01–E10)
│   └── tests/
├── pipelines/
│   ├── build.gradle.kts
│   ├── src/main/java/
│   │   └── com/datensee/
│   │       ├── DatensEEPipeline.java  ← Beam pipeline shell (kind-agnostic)
│   │       ├── PipelineConfig.java    ← Envelope record + PixelPayload nested
│   │       ├── options/               ← PipelineOptions
│   │       ├── fetch/                 ← Shared EE primitives (auth, error kind)
│   │       └── pixel/                 ← Pixel-pipeline subpackage
│   │           ├── PixelGrid.java, TileCoordinate.java, FetchedTile.java,
│   │           │   FailedTileRecord.java, …
│   │           ├── PixelOutputTransform.java  ← two-tier routing + terminal write
│   │           ├── fetch/             ← TileFetchDoFn, TileFetchTransform
│   │           └── io/                ← CogWriter, AssembledCogWriter, …
│   └── src/test/java/
├── contract/
│   ├── pipeline-config.schema.json  ← Discriminated by pipeline_kind
│   └── examples/
│       └── ndvi-california.json
├── notebooks/
│   └── datensee_quickstart.ipynb  ← End-to-end Colab example
└── docs/
```

### The Pixel/Vector Seam

`PipelineConfig` is shaped as an envelope plus a discriminated payload:

```jsonc
{
  "pipeline_kind": "pixel",           // future: "vector"
  "ee_expression": "...",             // shared
  "gee_project": "...",               // shared
  "runner": { ... },                  // shared
  "snapshot_time": 1715000000000000,  // shared (microseconds)
  "pixel": {                          // payload, required iff kind=="pixel"
    "tile_grid": { ... },
    "output": { ... }
  }
}
```

A vector pipeline will add `pipeline_kind="vector"` + a sibling `vector:` payload describing FeatureCollections, reducers, and Parquet output. The envelope fields stay identical. Inside the Beam pipeline, `DatensEEPipeline.expand()` validates the discriminator and delegates the terminal write stage to a kind-specific transform (today `PixelOutputTransform`; vector would add its own). The shared HV-API primitives — `EeApiException`, `EeAuthRemediation`, `EeErrorKind` — stay at `com.datensee.fetch` because both pipelines call the same High Volume endpoint family.

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
- `pyproj` — CRS handling for tiling logic
- `shapely` — geometry operations for region decomposition

### Java
- `org.apache.beam:beam-sdks-java-core`
- `org.apache.beam:beam-runners-google-cloud-dataflow-java`
- `org.apache.beam:beam-runners-direct-java` — local runner for testing/small jobs
- `com.google.cloud:google-cloud-storage` — GCS output
- `com.google.auth:google-auth-library-oauth2-http` — EE + GCP auth
- `com.fasterxml.jackson.core:jackson-databind` — config parsing

COG assembly is a pure-Java TIFF transcoder (`CogTranscoder`) — no GDAL dependency anywhere.

## Hard Technical Problems

These are the areas where the real complexity lives:

1. **Rate limiting the HV API.** The High Volume endpoint has per-project quotas. Resolved decision: there is **no client-side QPS limiter**. Tile fetches are I/O-bound, so throughput is shaped by worker parallelism (workers × harness threads) plus 429-driven exponential backoff in `TileFetchDoFn` — EE's own quota system is the rate-shaping signal. A client-side limiter sized below EE's actual capacity just starves the autoscaler. There is no rate-limit knob on the config surface at all — an advisory `max_qps` field existed briefly and was removed as ceremony.

2. **Tiling strategy.** Naive regular grids waste requests in sparse regions and can hit memory limits in dense ones. Adaptive tiling (quadtree decomposition based on data density or complexity) is a future optimization but adds significant complexity. Start with regular grids.

3. **Raster assembly at scale.** Stitching thousands of tiles into a single COG that's terabytes in size is non-trivial. The current shape: two-tier tiling (`output_tile_size_pixels`) lets users dial output granularity from "one COG per fetch" up to "one COG per region" without ever generating a manifest file. No VRT, no XML — output COGs are self-describing.

4. **Auth propagation.** EE auth tokens need to reach every Dataflow worker. Beam's credential propagation works for GCP services but EE auth is separate. May need to pass refresh tokens via pipeline options or use workload identity federation.

5. **Error handling at scale.** When 50 out of 10,000 tile fetches fail, the right behavior is retry → skip → report, not abort the whole job. Beam's built-in retry helps but the UX of partial failures needs design.

## Design Principles

1. **EE is the computation engine. We are the parallelism engine.** Never interpret or optimize the EE expression — but composing with it is fine (callers can `.clip()` their image themselves; `export()` deliberately does not clip — decompose-time region intersection already keeps out-of-region tiles out).
2. **The API is the product, the CLI is one surface.** `api.py` owns orchestration; `main.py` (CLI) and `notebook.py` (Colab/Jupyter) are thin display layers. Every rough edge is a user lost.
3. **Fail fast, fail loud.** Validate everything in Python before submitting the Dataflow job.
4. **EE users aren't infra engineers.** Abstract away Dataflow concepts behind opinionated defaults with escape hatches.
5. **COG to GCS is the primitive.** All other formats are GDAL post-processing.
6. **Precision is everything.** You'll need to make sure that everything aligns exactly. Don't accept pixel misaligment or projection mismatches.

## Core Design Decisions

The load-bearing decisions, in one place. Each is enforced by tests; don't change one side of a cross-language contract without the other.

- **`PixelGrid` is the canonical export shape.** Every export normalizes to a single `PixelGrid` (`{crs_code, affine_transform, dimensions}`) mirroring EE's own type, sent verbatim to `computePixels`. Tiles are integer pixel rectangles (`col_px`, `row_px`, `width_px`, `height_px`) within that parent grid; float bboxes are derived from `transform × pixel_offsets` and never persisted. `scale_meters` for geographic CRSs uses the equator constant (no latitude correction), so cross-export grid alignment is unconditional — same CRS + scale + tile size = identical pixel-to-CRS transform regardless of region. The parent grid is persisted in `_export_meta.json` so retry can reconstruct CRS coordinates from journal records' local pixel offsets.
- **Two-tier tiling and the origin contract.** Compute tiles (small, sized for the HV API) are decoupled from output COGs (large, sized for practical file counts) via `output.output_tile_size_pixels`: compute tiles group by output tile through a Beam `GroupByKey` and assemble into multi-block COGs whose internal block size equals the compute tile size. **The contract:** in two-tier mode `decompose_region` snaps the parent-grid origin to *output-tile* boundaries, so `out_row`/`out_col` are pure local arithmetic (`row_px // OTS`, `col_px // OTS`) and the Java assembler derives each output tile's origin from its `(out_row, out_col)` key — the two sides agree by construction.
- **The failures journal is the partial-failure model.** Fetch *and* write failures dead-letter into `{output}/_failures.json` (never fail the job); `datensee retry` classifies each record — EE complexity verdicts (HTTP 400 memory/timeout signatures only) split into 4 quadtree children, transient infra retries the same rect, terminal kinds carry over via the pipeline-side journal union. Every retry round sets `merge_existing_output=true`: the assembler decodes the existing COG as the baseline canvas and overlays re-fetched tiles (including sub-block split children), so splitting is legal for every export shape while fresh exports keep the direct no-shuffle writer. `datensee retry --until-done` drives rounds to steady state. Retry hard-requires the `_export_meta.json` sidecar (with its `pixel_grid`). See [`docs/retry-with-journal.md`](docs/retry-with-journal.md).
- **Snapshot pinning, in microseconds.** Every export captures one Unix-microseconds timestamp `T` at submit and pins every asset-load node's `version` arg to it, so all workers see one snapshot of mutable collections. `T` persists in the meta sidecar and is inherited by retries. BigQuery loads can't be pinned: `runBigQuery` is rejected unless its SQL contains `FOR SYSTEM_TIME AS OF`; `loadBigQuery` is rejected outright; `loadGeoTIFF` warns (no version mechanism exists). **Units note:** EE's `version` arg is compared against the asset's `system:version`, a *microsecond* timestamp — nanoseconds land in a range (`> ~1e17`) where EE crashes with `gRPC INTERNAL`, surfaced as silent client-side hangs. `pin_expression` rejects values above `1e17` and at or below `0` (EE's `-1` "latest" sentinel would silently defeat pinning); legacy nano values in old meta files migrate on read. A standalone wire-level reproducer for the EE-side bug lives at [`docs/ee-version-bug-reproducer.py`](docs/ee-version-bug-reproducer.py).
- **The wire contract is pinned by a test.** `contract/pipeline-config.schema.json` must exactly match the Pydantic models; `cli/tests/test_contract_schema.py` fails on any drift. The Java payload records are deliberately strict about unknown fields so a stale cached JAR fails loudly against a newer CLI's config instead of silently ignoring semantics like `merge_existing_output`.

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
