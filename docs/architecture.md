# DatensEE: Architecture & Conventions

High-level architecture, core design decisions, and coding conventions for DatensEE.


## Overview

DatensEE parallelizes Google Earth Engine image exports across Cloud Dataflow
workers using Earth Engine's High Volume API (`computePixels`). Rather than
recompiling or interpreting expression graphs, DatensEE treats computations as
opaque JSON, decomposes the target region into an aligned tile grid, fetches
tiles concurrently across distributed workers, and writes Cloud Optimized
GeoTIFFs (COGs) to Google Cloud Storage or local disk.

## Architecture Overview

```
                          ┌──────────────────────────────┐
                          │     Earth Engine Backend      │
                          │  (evaluates computation      │
                          │   per-tile via HV endpoint)  │
                          └──────────▲───────────────────┘
                                     │ High Volume API
                                     │ (concurrent tile fetches)
                                     │
┌──────────────┐          ┌──────────┴───────────────────┐          ┌────────────┐
│  Python CLI  │─────────▶│     Dataflow / Local Runner   │─────────▶│  GCS       │
│              │ submits   │                               │ writes   │  (COG)     │
│  • Parse EE  │ job      │  Create tile coords           │ output   │            │
│    params    │          │  → ParDo: fetch tile (HV API) │          │  or Local  │
│  • Tile the  │          │  → Assemble raster            │          │    Disk    │
│    region    │          │  → Write COG to GCS           │          │            │
│  • Submit    │          │                               │          │            │
└──────────────┘          └───────────────────────────────┘          └────────────┘
```

### Execution Flow

1. **User inputs:** Serialized EE expression, region geometry, scale or exact
   grid transform, and output destination.
2. **Python control plane:**
   - Validates inputs and pins asset versions via `snapshot_time`.
   - Decomposes the region into a snapped tile grid in the target CRS.
   - Submits a Dataflow Flex Template job or runs locally via DirectRunner.
3. **Java Beam pipeline:**
   - Ingests tile coordinates from inline config or an NDJSON tiles file.
   - Fetches tiles concurrently from the EE High Volume API with exponential
     backoff on HTTP 429 responses.
   - Transcodes responses to COG (tiled layout, Deflate compression) via a
     pure-Java transcoder.
   - Assembles multi-block rasters in two-tier mode and writes COGs to storage.
   - Records any failed tiles in `{output}/_failures.json`.
4. **Validation and recovery:**
   - Output COGs are self-describing GeoTIFFs readable directly by GIS tools.
   - `datensee validate` checks output structural integrity.
   - `datensee retry --until-done` resolves failed tiles recorded in the journal.

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

The public Python API lives in `api.py`; the CLI (`main.py`) is a thin wrapper. Key functions:

- `datensee.export(ee_expression, region, project, output, ...)` → `ExportResult`: full export pipeline
- `datensee.demo(project, output)` → `ExportResult`: built-in NDVI demo
- `datensee.tile(region, scale, crs, tile_size)` → `TileGrid`: region decomposition only
- `datensee.poll(job_id, project, region, callback=...)` → `JobState`: Dataflow job polling

`notebook.py` provides Colab/Jupyter adapters:

- `notebook.ensure_auth()`: triggers `google.colab.auth` when ADC unavailable
- `notebook.ensure_jar()`: auto-downloads JAR if not found locally
- `notebook.display_job_progress(job_id, ...)`: HTML polling display
- `notebook.display_export_summary(config)`: HTML config summary

`submit.py` and `status.py` accept optional callbacks (`progress_callback`, `status_callback`) so the notebook layer can replace Rich with HTML rendering without touching business logic.

### The Contract (`/contract`)
The pipeline config passed from Python → Java. Defines:
- EE computation description (opaque serialized expression: we don't interpret this)
- Tile grid: list of tile coordinates + CRS + scale
- Output: GCS path, COG parameters
- Runner config: Dataflow project/region/worker settings OR local mode flag

## Repository Structure

The shape splits cleanly between *shared infrastructure* (auth, snapshot pinning, Dataflow submission, job polling, the runner-agnostic envelope of `PipelineConfig`) and a *pixel pipeline subpackage* (`datensee.pixel` / `com.datensee.pixel`) that owns the raster-specific machinery: tile decomposition, the EE `computePixels` fetcher, COG output, and the raster-shaped Pydantic / record models. The seam exists so a future vector pipeline (EE `computeFeatures` → reduceRegions → GeoParquet) can land as a sibling subpackage without disturbing the pixel surface. See "The Pixel/Vector Seam" below for the wire-format contract.

```
datensee/
├── LICENSE
├── cli/
│   ├── pyproject.toml
│   ├── src/
│   │   └── datensee/
│   │       ├── __init__.py      ← Re-exports public API (export, demo, poll, tile)
│   │       ├── api.py           ← Public Python API: orchestration logic
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

A vector pipeline will add `pipeline_kind="vector"` + a sibling `vector:` payload describing FeatureCollections, reducers, and Parquet output. The envelope fields stay identical. Inside the Beam pipeline, `DatensEEPipeline.expand()` validates the discriminator and delegates the terminal write stage to a kind-specific transform (today `PixelOutputTransform`; vector would add its own). The shared HV-API primitives (`EeApiException`, `EeAuthRemediation`, `EeErrorKind`) stay at `com.datensee.fetch` because both pipelines call the same High Volume endpoint family.

## Coding Conventions

Write clean, typed, functional code with explicit contracts and immutable data structures where practical.

### Python
- Python 3.12+ (use modern syntax: `X | Y` unions, `match` statements)
- Type hints on every function signature without exception
- Pydantic v2 for all data models (use `model_validator`, not legacy decorators)
- Typer for CLI with explicit `typer.Option()` / `typer.Argument()` annotations
- Format with `ruff format`, lint with `ruff check`
- Tests: pytest with fixtures, no unittest-style classes
- Prefer `pathlib.Path` over `os.path`
- Use `httpx` for HTTP calls, not `requests`
- Docstrings: Google style

### Java
- Java 25 source, targeting Java 21 bytecode (`--release 21`) for Dataflow worker compatibility
- Gradle Kotlin DSL (`build.gradle.kts`)
- Beam SDK conventions: PTransforms are top-level classes, not anonymous lambdas
- Use records for immutable data types (tile coordinates, fetch results, config)
- Tests: JUnit 5 + `TestPipeline` from Beam testing utilities
- Google Java Style (enforced via Checkstyle)

### Cross-Cutting
- Commit messages: Conventional Commits (`feat:`, `fix:`, `chore:`, etc.)
- No abbreviations in public APIs
- Error messages must state what failed and how to remediate it

## Key Dependencies

### Python
- `typer[all]`: CLI framework
- `pydantic>=2.0`: Config validation
- `httpx`: HTTP client for Dataflow API and Earth Engine calls
- `rich`: Terminal output, progress bars, log streaming
- `pyproj`: CRS handling and transformation
- `shapely`: Geometry operations for region decomposition

### Java
- `org.apache.beam:beam-sdks-java-core`
- `org.apache.beam:beam-runners-google-cloud-dataflow-java`
- `org.apache.beam:beam-runners-direct-java`: Local runner for testing and small jobs
- `com.google.cloud:google-cloud-storage`: GCS output
- `com.google.auth:google-auth-library-oauth2-http`: EE and GCP authentication
- `com.fasterxml.jackson.core:jackson-databind`: Configuration parsing

COG assembly uses a pure-Java TIFF transcoder (`CogTranscoder`), eliminating any GDAL dependency on workers.

## Core Subsystems

1. **Rate shaping via backoff:** The High Volume API enforces per-project quotas. There is intentionally no client-side rate limiter. Throughput is governed by worker parallelism (`num_workers × harness_threads`) combined with exponential backoff on HTTP 429 responses in `TileFetchDoFn`.
2. **Deterministic tiling:** Regions decompose into regular grids snapped to `(0, 0)` in the target CRS. Compute tiles are sized for High Volume API throughput (typically 512x512 pixels).
3. **Raster assembly:** In two-tier mode (`output_tile_size_pixels`), compute tiles are shuffled by output tile key and assembled into multi-block COGs using `AssembledCogWriter`. Output COGs are self-describing standard GeoTIFFs.
4. **Credential delegation:** Dataflow workers use the worker Compute Engine service account (or an impersonated service account via `--eeImpersonateSa`). In service environments, caller OAuth tokens pass to the JVM via an inheritable pipe file descriptor (`--userTokenFd`).
5. **Partial failure handling:** Failed tile requests dead-letter into `{output}/_failures.json`. `datensee retry` inspects the failure records, retrying transient network errors and splitting memory/timeout errors into four quadrant sub-tiles.

## Engineering Principles

1. **Opaque computation delegation:** Earth Engine evaluates expressions per tile; DatensEE does not inspect, recompile, or optimize expressions. Callers can clip images prior to export if server-side masking is desired.
2. **API-first architecture:** `api.py` contains core orchestration logic; `main.py` (CLI) and `notebook.py` (Colab/Jupyter) provide presentation and user interaction layers.
3. **Client-side preflight validation:** Validate geometry, projections, and configuration parameters in Python prior to Dataflow job submission.
4. **Targeted operational defaults:** Abstract Dataflow configuration behind tested defaults (such as worker harness thread counts tuned for I/O-bound requests) while preserving configuration overrides.
5. **Direct COG generation:** Target Cloud Optimized GeoTIFF as the primary output format, enabling direct ingestion by standard GIS tooling and Earth Engine (`Image.loadGeoTIFF`).
6. **Strict geometric alignment:** Enforce integer pixel grid alignment against the target CRS origin across all exports.

## Core Architectural Invariants

- **`PixelGrid` as canonical shape:** Every export normalizes to a single `PixelGrid` (`{crs_code, affine_transform, dimensions}`). Tiles are integer pixel rectangles (`col_px`, `row_px`, `width_px`, `height_px`) within that parent grid. For geographic CRSs, `scale_meters` converts using the equator constant (no latitude correction), ensuring cross-export grid alignment. The parent grid is persisted in `_export_meta.json` so retries reconstruct CRS coordinates accurately.
- **Two-tier origin alignment:** In two-tier mode, `decompose_region` snaps the parent-grid origin to output-tile boundaries. `out_row` and `out_col` are derived by local arithmetic (`row_px // OTS`, `col_px // OTS`). The Java assembler derives each output tile's origin directly from `(out_row, out_col)`.
- **Failures journal accounting:** Fetch and write failures dead-letter into `{output}/_failures.json`. `datensee retry` classifies each failure: complexity verdicts (`MEMORY_EXCEEDED`, `COMPUTATION_TIMEOUT` on HTTP 400) trigger quadtree splitting, transient failures retry the original rect, and terminal kinds carry over. Retry rounds set `merge_existing_output=true`, decoding existing COGs and overlaying recovered tiles in place.
- **Snapshot pinning in microseconds:** Exports record a Unix microsecond timestamp `T` at submission and pin all asset-load nodes to `T`. Values exceeding $10^{17}$ are rejected to prevent integer overflow crashes in Earth Engine's version comparison.
- **Contract schema parity:** `contract/pipeline-config.schema.json` must match the Pydantic models exactly; `cli/tests/test_contract_schema.py` validates schema parity on every test run. Java records enforce strict field validation to detect client/worker version mismatches immediately.

## Testing

### Integration tests (EE HV API)

GCP project for testing: **`datensee-testing`** (registered for non-commercial EE use).

Run integration tests:
```bash
cd cli && uv run pytest tests/test_integration_ee.py --integration --gee-project=datensee-testing -v
```

Tests use SRTM DEM (`USGS/SRTMGL1_003`), pre-cached and cheap but spatially varying,
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

## Design rationale: why it isn't fifty lines

A single-machine script using a thread pool and `rasterio` can fetch tiles from
Earth Engine's High Volume API and mosaic them up to several gigabytes. DatensEE
is engineered for the regime beyond that threshold: terabyte-scale exports,
distributed worker execution, and jobs where partial failure must be handled
deterministically.

The architecture reflects three core design constraints:

### 1. Zero Native Dependencies on Workers

Standard Cloud Dataflow worker environments do not include GDAL, and
maintaining custom container images with native C++ dependencies adds
operational complexity and deployment overhead.

To eliminate native dependencies on workers:
- COG generation uses a pure-Java TIFF transcoder (`CogTranscoder`) built on
  standard Java libraries.
- Two-tier raster assembly and retry merging use a streaming canvas assembler
  (`AssembledCogWriter`) rather than in-memory GDAL mosaics.

### 2. Fault Tolerance and Iterative Recovery

At scales of thousands of tile requests, network partitions, quota throttling,
and server-side memory limits are expected events. DatensEE avoids aborting
whole jobs on partial failures:
- **HTTP 429 handling:** Employs exponential backoff with jitter, using Earth
  Engine's quota limits as the pacing mechanism.
- **Structured error classification:** Inspects response status codes and
  failure signatures to distinguish transient infrastructure issues from
  computation complexity limits.
- **Failures journal:** Failed tile coordinates dead-letter into a structured
  journal (`_failures.json`) rather than halting the pipeline.
- **Adaptive quadtree splitting:** Tiles that fail due to Earth Engine memory
  or execution timeouts are split into four quadrant sub-tiles by
  `datensee retry`.
- **Canvas merging:** Retry rounds set `merge_existing_output`, decoding
  previously written COGs and overlaying recovered tiles in place.

### 3. Deterministic Geometry and Asset Consistency

Distributed tile fetching introduces subtle synchronization and geometric
consistency challenges:
- **Integer grid alignment:** Every tile is computed as an integer pixel
  rectangle anchored to a global origin in the target CRS. Sub-tiles produced
  by retry splits align directly with existing parent block boundaries.
- **Snapshot pinning:** Collection updates during a multi-hour export could
  cause adjacent tiles to evaluate against different underlying source
  imagery. DatensEE stamps an explicit microsecond timestamp onto every
  asset-loading node in the expression graph, ensuring all workers evaluate
  against an identical data snapshot.

### Scope and Boundaries

DatensEE focuses strictly on parallel export orchestration and raster assembly.
It does not provide:
- A custom computation DSL or query planner (Earth Engine remains the
  computation engine).
- Spatial analysis or data cataloging features.

For workloads that fit within standard batch export limits or single-node
memory, [xee](https://github.com/google/xee) or `Export.image.toCloudStorage`
remain the recommended tools.
