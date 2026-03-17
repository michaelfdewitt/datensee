# CLAUDE.md — DatensEE Orchestrator

## What This Project Is

A CLI tool that lets Google Earth Engine users run image exports at massive scale by parallelizing tile fetches across Google Cloud Dataflow workers. Users provide the same computation description they already use in Earth Engine — the tool handles tiling, parallel fetching via the EE High Volume API, assembly into Cloud Optimized GeoTIFFs, and upload to GCS.

**The key insight:** We don't need to understand or recompile EE computations. Earth Engine evaluates its own expression graph per-tile — we just need to call it a lot, in parallel, and stitch the results together. This is a massively parallel tile fetcher with smart orchestration, not a computation framework.

## Architecture Overview

```
                          ┌──────────────────────────────┐
                          │     Earth Engine Backend      │
                          │  (evaluates computation       │
                          │   per-tile via HV endpoint)   │
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
   - Assembles fetched tiles into Cloud Optimized GeoTIFF(s)
   - Writes to GCS
4. **Post-processing (optional):** GDAL translate for format conversion (separate step)

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
├── cli/
│   ├── pyproject.toml
│   ├── src/
│   │   └── datensee/
│   │       ├── __init__.py
│   │       ├── main.py          ← Typer app entrypoint
│   │       ├── config.py        ← Pydantic models for pipeline config
│   │       ├── tiling.py        ← Region → tile grid decomposition
│   │       ├── submit.py        ← Dataflow job submission
│   │       └── status.py        ← Job polling, log streaming
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

3. **Raster assembly at scale.** Stitching thousands of tiles into a single COG that's terabytes in size is non-trivial. May need to output tile-pyramid COGs or multiple files with a VRT manifest. GDAL's COG driver handles this but needs memory management.

4. **Auth propagation.** EE auth tokens need to reach every Dataflow worker. Beam's credential propagation works for GCP services but EE auth is separate. May need to pass refresh tokens via pipeline options or use workload identity federation.

5. **Error handling at scale.** When 50 out of 10,000 tile fetches fail, the right behavior is retry → skip → report, not abort the whole job. Beam's built-in retry helps but the UX of partial failures needs design.

## Design Principles

1. **EE is the computation engine. We are the parallelism engine.** Never interpret or optimize the EE expression — just fetch tiles.
2. **The CLI is the UX.** Every rough edge is a user lost. Invest in error messages, progress feedback, and sensible defaults.
3. **Fail fast, fail loud.** Validate everything in Python before submitting the Dataflow job.
4. **EE users aren't infra engineers.** Abstract away Dataflow concepts behind opinionated defaults with escape hatches.
5. **COG to GCS is the primitive.** All other formats are GDAL post-processing.

## Current Status

🟡 **Project bootstrap** — Setting up toolchain and initial scaffolding.

## Milestones

1. **M1: Proof of Life** — CLI takes a hardcoded NDVI expression + small region, tiles it, submits to Dataflow, fetches tiles via HV API, writes a single GeoTIFF to GCS.
2. **M2: Real Config** — Pipeline config schema defined. CLI accepts arbitrary EE expressions, regions, scales. Local runner works for small jobs.
3. **M3: Scale** — Rate limiting, retry logic, adaptive tiling, COG output, large-region support.
4. **M4: UX Polish** — Rich progress output, log streaming, `status` / `logs` / `cancel` subcommands, cost estimation.
5. **M5: Distribution** — `pip install datensee`, prebuilt pipeline JARs, documentation, quickstart guide.

## Notes for Claude

- When generating code, always include type hints (Python) or type annotations (Java). No untyped code.
- Prefer small, composable functions/methods over large monolithic ones.
- When in doubt about a Beam API, check https://beam.apache.org/documentation/ — don't guess.
- When in doubt about the EE HV API, check https://developers.google.com/earth-engine/reference — don't guess.
- The user is a staff engineer. Skip boilerplate explanations; be direct and precise.
- If a design decision has tradeoffs, name them explicitly rather than picking silently.
- The EE computation is opaque. Never attempt to parse, optimize, or interpret the EE expression graph.