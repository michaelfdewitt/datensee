# Handoff Notes

A reference for an agent picking up DatensEE for polish work. Read after `CLAUDE.md`.

`CLAUDE.md` already covers the high-level architecture, milestones, and conventions; do not duplicate it. This file captures **non-obvious decisions, recent fixes, current invariants, and known limitations** that you would otherwise have to spelunk for.

---

## Deployment story (read first)

DatensEE is being checked in to the **`earthengine` monorepo** under `tools/datensee/`. It is published to PyPI as **`datensee`**.

The two facts the next agent must internalize:

1. **Users install with `pip install datensee`.** That's the well-lit path. The CLI auto-downloads the pipeline JAR on first run. No `gradle`, no `uv sync`, no Java installation, no manual JAR placement. The README's "Development setup" section is the only place where Gradle/uv appear, and it's labeled as such.
2. **`datensee` does NOT depend on `earthengine-api`.** The two packages live in the same monorepo but are deliberately decoupled. We accept EE expressions as **opaque serialized JSON** — we never call `ee.serializer.encode` ourselves at runtime. Users who want to author expressions can install `earthengine-api` separately. **Do not add an `earthengine-api` dependency to `cli/pyproject.toml`** — that would balloon the install footprint and pin users to a specific EE client version.

Concretely the dep set is small: typer, pydantic, httpx, rich, jsonschema, pyproj, shapely, google-auth, google-cloud-storage, requests. Optional extras: `[validation]` adds rasterio, `[notebook]` adds matplotlib. Keep it that way.

When you make changes that affect the install surface, update `cli/pyproject.toml` and the README's "Install" section in lockstep. The README is the source of truth for what users see; the handoff doc and CLAUDE.md are for developers.

---

## Repo orientation (one-line per directory)

- `cli/` — Python package `datensee` (Typer CLI + Python API). Owns tiling, submission, polling, output validation.
- `pipelines/` — Java/Beam pipeline (Gradle Kotlin DSL). Owns the per-tile fetch, COG transcoding, and GCS write.
- `service/` — FastAPI Cloud Run wrapper around `datensee.api.export`. Used by Foundree.
- `contract/` — JSON schema for the Python → Java pipeline config.
- `notebooks/` — Colab quickstart.
- `docs/` — `validation.md` (output-validation check catalog), this file.

The Python side **submits** (validates, tiles, spawns the JVM) and **observes** (polls Dataflow). The Java side **does the actual export work**.

---

## COG pipeline — the part the user cares about

The COG transcoder is the single highest-risk piece of code in the repo. EE's `Image.loadGeoTIFF` is a strict validator; if a COG is not perfectly formed, every pixel comes back masked and you don't notice until users complain.

### Data flow
```
EE HV API (computePixels)
   → raw GeoTIFF — TILE-layout, Adobe Deflate (compression code 32946),
     internal tiles 256×256 max. For requests up to 256×256 the response
     contains exactly one inner tile; for larger requests EE chunks into
     a 256×256 tile grid (e.g. 4 tiles for 512×512, 9 for 768×768).
     Each tile is its own self-contained zlib stream — extractPixelData
     must inflate per-chunk, not feed the concatenation into a single
     Inflater. Verified empirically by cli/scripts/probe_hv_dimensions.py.
TileFetchDoFn — fetches and dead-letters on retryable failures
   → FetchedTile (raw bytes + coordinate)
TileWriterDoFn (or AssembledCogWriter in M6 mode) — calls CogTranscoder,
   writes to GCS or local disk
   → COG file per output tile, named tile_r{row:04d}_c{col:04d}.tif
```

The output of the pipeline is a directory of COG files, one per output tile. There is no manifest file (no VRT, no JSON sidecar, no XML) — output COGs are self-describing via standard GeoTIFF tags, and any modern GIS tool (QGIS, rasterio, ArcGIS) opens a directory of geotagged TIFFs as a layer set without help. Users who want a single stitched COG control granularity via `output_tile_size_pixels`: set it large enough to cover the export region and the pipeline emits one COG; leave it small (or unset) and the pipeline emits many.

### Multi-block COGs (M6)

`CogTranscoder` emits multi-block COGs. The image dimensions must be a whole-number multiple of `tileSize` (the COG's internal block size); partial-edge tiles aren't supported. For one-COG-per-fetch (the default mode), `imageWidth == imageHeight == tileSize` and there's a single block. For M6 two-tier tiling, each compute tile becomes one inner COG block: the assembler extracts pixels per compute tile, places them by `(tx, ty)` block index in a row-major `List<byte[]>` (null entries are zero-filled), and `CogTranscoder` compresses each block independently per the TIFF spec — no intermediate `output_tile_size × output_tile_size` buffer.

Two entry points:
- `transcode(rawGeotiff, tileSize, compression)` — input is an EE-HV-shaped GeoTIFF; used by the one-COG-per-tile path.
- `transcodeFromTileBlocks(tilePixels, width, height, tileSize, sourceTiffForMetadata, outputTileOriginX, outputTileOriginY, compression)` — input is a row-major list of per-block pixel buffers (length `(width/tileSize) * (height/tileSize)`; `null` entries become zero-filled blocks) plus a representative compute tile for CRS/sample-structure metadata. Used by [`AssembledCogWriter`](../pipelines/src/main/java/com/datensee/io/AssembledCogWriter.java). The source tile's `ModelTiepoint` is overridden with the output tile's origin; everything else (`ModelPixelScale`, `GeoKeyDirectoryTag`, `GeoAsciiParams`, etc.) is inherited.

### Compression: deflate or none

`CogTranscoder` accepts two output compressions: `"deflate"` (default — zlib via `java.util.zip`) and `"none"`. Anything else fails fast inside `partitionCompressBuild`. The previous hand-rolled LZW encoder was deleted in favor of deflate-only since the LZW output didn't pass strict TIFF decoders (GDAL / imagecodecs / EE's loader) and deflate is well-supported everywhere downstream. The transcoder still decodes deflate-compressed *inputs* in case the EE HV API ever returns one. No predictor encoding is applied.

### COG layout (the order matters)

A valid COG **must** have:
```
[0..7]    TIFF header → IFD offset = 8
[8..]     First IFD (count + entries + next-IFD pointer = 0)
[..]      Overflow tag data (GeoKeys, ModelPixelScale, ModelTiepoint, …)
[..]      Pixel data (one tile in our case)
```

A vanilla TIFF writer puts the IFD at the end; that's a valid TIFF but EE rejects it with "The first IFD does not immediately follow the TIFF header." `buildCogTiff` does this in two passes — sizing then serialization — because `TileOffsets` has to point at the pixel-data offset, which depends on IFD+overflow size.

Tests pin the layout (`transcodedFileHasFirstIfdAtOffsetEight`, `tileOffsetsPointPastTheIfdIntoPixelData`, `outputDeclaresTileLayoutAndDropsStripTags`) and pixel-data round-trips (uint8/uint16/int16/float32, multi-band, deflate input, GeoTIFF metadata preservation). See `pipelines/src/test/java/com/datensee/io/CogTranscoderTest.java`.

### Planar configuration rejected

If `PlanarConfiguration` (tag 284) is present and not 1 (chunky), `CogTranscoder` throws. Our pixel extraction concatenates strip/tile bytes verbatim, which only round-trips for chunky. EE HV normally returns chunky; planar would indicate an unexpected fetch shape.

### Trailing-slash bug (don't undo this)

`TileWriterDoFn.writeToGcs` strips both leading and trailing slashes from the prefix. The reason: Foundree's `ExportService.outputPath()` passes `gs://bucket/pfx/` with a trailing slash, and we used to concatenate `prefix + "/" + tifName` → `pfx//tile.tif`. EE's GCS connector normalizes `//` → `/` before the GET, so the object couldn't be found and every pixel came back masked. **The strip is load-bearing.** If you "clean up" that regex, you will silently break Foundree.

---

## Auth — the other minefield

Two mostly-orthogonal stories:

1. **Standalone CLI** — `gcloud auth application-default login` provides ADC. Java picks it up via `GoogleCredentials.getApplicationDefault()`.
2. **Service-driven (Foundree, future tools)** — Caller has an OAuth access token belonging to the **end user**, not the service account. Token must reach the JVM without ever appearing on argv (`/proc/<pid>/cmdline`) or env (`/proc/<pid>/environ`).

The service-driven path uses an **inheritable pipe FD**:
- Python (`cli/src/datensee/submit.py`) creates `os.pipe()`, writes the token to the write-end, closes write, marks read-end inheritable, passes `--userTokenFd=<N>` to the JVM.
- Java (`DatensEEPipeline.applyUserCredentials`) reads `/proc/self/fd/<N>` (Linux-only — fine, Dataflow workers and Cloud Run are Linux), wraps the token in `QuotaProjectUserAccessTokenCredentials`, and installs it as the GCP credential on `GcpOptions`.

`QuotaProjectUserAccessTokenCredentials` exists because:
- The token is a bearer credential — there's no refresh path. We override `refreshAccessToken()` to a no-op; the caller guarantees freshness.
- `GoogleCredentials.createWithQuotaProject()` only injects `x-goog-user-project` in the no-arg `getRequestMetadata()`, but Beam's `HttpCredentialsAdapter` calls the URI-taking variant. Without the override, the header doesn't reach the Dataflow `createJob` call, and Google attributes quota + API-enablement to the OAuth client's implicit project (Foundree's app project), which doesn't have Dataflow enabled. **Don't simplify this back to `createWithQuotaProject()`** — it has been verified empirically to drop the header in this google-auth-library version.

---

## Recent significant fixes (April 2026, top of `master`)

| Commit | Why it was needed |
| --- | --- |
| `17b8c32` fix(pipelines): COG layout, deflate default, strip trailing slash | EE rejected our COGs ("first IFD does not immediately follow TIFF header"); LZW corrupt; double-slash blob names returned masked pixels. |
| `ce16820` surface pipeline JVM errors and use createWithQuotaProject | `subprocess.run(capture_output=True)` was eating the JVM's stack trace. Now we stream live + keep a bounded tail. |
| `346d9d5` bind user credentials to target project as quotaProject | Dataflow `createJob` was hitting Foundree's app project for enablement checks. |
| `cced6cd` accept runner.dataflow.labels in PipelineConfig | Schema mismatch was silently dropping job labels. |
| `e099b2f` repair Dockerfile uv invocation and trim build context | Cloud Run image build was broken. |

Uncommitted on disk (April 2026):

- `cli/src/datensee/expression.py` — reject ImageCollection results with a clear error in `clip_expression`. The user got a Dataflow job that dead-lettered every tile when they forgot to `.median()`.
- `cli/src/datensee/submit.py` — switched from `subprocess.run(capture_output=True)` to `Popen` with line-by-line streaming; parses `DATENSEE_JOB_ID=<id>` from stdout to return the actual Dataflow job ID instead of `None`.
- `service/src/datensee_service/app.py` — adds a Spanner-backed `/submit-task` endpoint (Foundree's task queue target). Reads payload by `(user_id, task_id)`, updates state, returns 200 even on business errors so Cloud Tasks doesn't retry validation failures.
- `service/pyproject.toml` — `google-cloud-spanner` dep for the above.
- `cli/tests/test_expression.py` — tests for the ImageCollection rejection.

---

## Adaptive retry — implemented

The failures journal (`{output}/_failures.json`) is structured NDJSON of `FailedTileRecord` (superset of `TileCoordinate` with `error_kind`, `attempts`, timestamps, `lineage`). `EeErrorKind.classify(httpStatus, body)` (Java) populates the kind from the EE HV response; the dead-letter side output is typed `FailedTileRecord`. `datensee retry --journal _failures.json` reads it back, splits split-eligible failures into 4 quadrant children (lineage extended with quadrant index 0–3, CRS-axis-order-independent), retries transient failures with the same bbox, and submits a fresh pipeline run via `tile_grid.tiles_file`. Default max depth is 2 (one root → 16 sub-tiles max).

**Conservative split allowlist:** only `MEMORY_EXCEEDED` and `COMPUTATION_TIMEOUT`. Generic 5xx, rate-limit, auth errors never trigger splitting — that's intentional and pinned in tests. Adding a kind is a one-line config change later; removing one that's already triggering production cascades is a fire.

`TileCoordinate.lineage` defaults to `[]` and is informational on the success path — the assembler keys on bbox geometry, not lineage. Lineage is consumed by the retry-decision logic and shows up in the journal for human triage.

Full design + retry semantics in [`docs/retry-with-journal.md`](retry-with-journal.md).

---

## M6 two-tier tiling — wiring summary

Compute tiles flow into the pipeline as before. When `output.output_tile_size_pixels` is set on `OutputConfig`, `DatensEEPipeline` swaps the per-tile writer (`CogWriter`) for `AssembledCogWriter`, which:

1. Keys each `FetchedTile` by `(out_row, out_col)` (assigned in `tiling.decompose_region` from `output_tile_size_pixels // tile_size_pixels`).
2. `GroupByKey` shuffles compute tiles together by output tile.
3. `AssembleAndWriteDoFn` allocates an output buffer of size `output_tile_size × output_tile_size`, copies each compute tile's pixel data into the right offset (computed from bbox math, CRS-axis-order-independent), and calls `CogTranscoder.transcodeFromAssembledPixels`.
4. The resulting multi-block COG is written as `tile_r{out_row:04d}_c{out_col:04d}.tif`.

The internal block size of the output COG is the compute tile size, so EE's `loadGeoTIFF` can random-access individual compute-tile-sized regions efficiently.

`VrtAssembler` is two-tier-aware: in M6 mode it deduplicates compute tiles by `(out_row, out_col)`, takes the union bbox per output tile, and writes one `<SimpleSource>` per output COG referencing that file's `output_tile_size`.

When `output_tile_size_pixels` is unset (the default), routing falls through to the existing one-COG-per-tile path with no behavior change.

---

## Known limitations / TODOs in priority order

1. **Geographic-CRS pixel size is approximated as `1/111_320` deg/m at the equator.** [`tiling._pixel_size_native`](../cli/src/datensee/tiling.py) hard-codes a flat degrees-per-meter conversion for `EPSG:4326` (and any other geographic CRS). Tile widths in degrees stay constant across latitude even though the corresponding ground distance does not, so a tile in northern Canada covers a much smaller patch of land than a tile at the equator at the same `scale_meters`. The existing snap/alignment tests assert per-tile uniformity in degrees, which by construction can't both hold and the approximation be correct. Fixing this needs a design call: (a) reject `EPSG:4326` exports above some latitude band and require a projected CRS, (b) compute the conversion at the region centroid, or (c) per-pixel scale via a proj transform. Worth doing before we ship workflows that span large latitude ranges in 4326.
2. **No automatic retry-loop driver.** `datensee retry` does one round per invocation. A wrapper that loops with backoff until the journal is empty is a small follow-up — not done because it would change the CLI UX surface and we want a clean checkpoint first.
3. **Dataflow / GCS carryover merge isn't wired yet — planned for the pipeline side.** Local-mode retry is the supported path today; in Dataflow mode the carryover (terminal + depth-capped records) is logged + dropped between rounds rather than appended to `_failures.json`. The fix is to do the merge in the Java pipeline, not in a post-step: the retry CLI will write `{output}/_carryover.json` alongside `_retry_tiles.json`, and the pipeline's failures-journal `TextIO.write` will union that file with this round's new failures before writing `_failures.json`. Same code path local + Dataflow; no orchestration on the Python side. Tracked as the next adaptive-retry milestone in `CLAUDE.md`. See `docs/retry-with-journal.md` for the design rationale.
4. **`/proc/self/fd/<N>` is Linux-only.** Service-driven auth path won't work on macOS or Windows. Standalone CLI on those OSes uses ADC and is fine.
5. **Dataflow job-id extraction uses reflection against a deprecated API.** [`DatensEEPipeline.run`](../pipelines/src/main/java/com/datensee/DatensEEPipeline.java) calls `getJobId` on the result via reflection so we don't pull `beam-runners-google-cloud-dataflow-java` into the compile classpath of every consumer; the current Beam version emits a deprecation warning at compile time. Two ways out: (a) add the runtime dep at compile time and call `((DataflowPipelineJob) result).getJobId()` directly, accepting the dep bloat, or (b) read the job id from the runner's stdout/stderr ahead of `waitUntilFinish` — already partially done via the `DATENSEE_JOB_ID=` echo. Pick one when the Beam minor version next bumps and the deprecation graduates to removal.
6. ~~`extractPixelData` for tile-layout inputs concatenates tile bytes in tile order, not pixel-row order. Safe today because EE HV always returns strip layout; would silently produce wrong pixels for a multi-tile input.~~ **Both halves of this comment turned out to be wrong.** EE HV does *not* return strip layout — it returns tile layout (256×256 internal tiles, deflate-compressed) for every request. And the failure mode wasn't "silently wrong pixels": each tile is its own zlib stream, so feeding the concatenation into a single `Inflater` stopped at the first end-of-stream marker, dropped every tile after the first, and crashed `extractBlock` downstream with `ArrayIndexOutOfBoundsException`. Fixed: `extractPixelData` now decompresses each chunk independently and assembles tile-layout inputs into row-major order. Pinned by `CogTranscoderTest#multiTileDeflateInputRoundTripsCorrectly` (synthesizes EE's actual response shape: 4-tile 2×2 grid of 256×256 deflate float32) and documented by `cli/scripts/probe_hv_dimensions.py`.
7. **Partial output tiles are zero-filled.** If some compute tiles in an output group failed and were dead-lettered, the assembler emits a partially-populated COG with zero-filled gaps. Whether to skip the output tile entirely or surface a warning is a design decision left for the next iteration.
8. **Predictor encoding is not applied for any compression.** Horizontal differencing on integer-typed bands would give a free compression-ratio win for deflate; we don't do it today. Pure ratio question, no correctness issue.

---

## Test runbook

Java unit tests (no network):
```bash
cd pipelines && ./gradlew test
```

Python unit tests (no network):
```bash
cd cli && uv run pytest
```

Python integration tests (real EE HV API; uses `datensee-testing` GCP project):
```bash
cd cli && uv run pytest tests/test_integration_ee.py --integration --gee-project=datensee-testing -v
```

Output validation suite (synthetic GeoTIFFs, requires `[validation]` extra for rasterio-backed checks):
```bash
cd cli && uv run --extra validation pytest tests/test_validation_output_unit.py
```

The COG-specific suites are `pipelines/src/test/java/com/datensee/io/CogTranscoderTest.java` (single-tile transcode round-trips) and `AssembledCogWriterTest.java` (M6 multi-block assembly + the 256-tile pixel-perfect validation case). Both decode COG output via `TestTiffReader` ([source](../pipelines/src/test/java/com/datensee/io/TestTiffReader.java)) — a separate test-only reader deliberately decoupled from `CogTranscoder`'s own decoder paths so a bug in the writer can't mask itself by being read back through the same broken assumptions. **Do not collapse the test reader into `CogTranscoder`'s helpers**; the independence is the point.

---

## Conventions worth knowing

- Python: type hints on every signature, Pydantic v2, `pathlib.Path`, `httpx`, Google-style docstrings. Run `ruff format` and `ruff check` before committing.
- Java: records + sealed interfaces + pattern matching. Java 25 source, but `--release 21` for Dataflow workers (don't accidentally use Java 22+ APIs). Google Java Style via Checkstyle.
- Errors are user-facing: say what went wrong AND what to do (e.g. `clip_expression`'s "Reduce the collection first (e.g. .median(), .mosaic(), .first())").
- Conventional Commits.
- `CLAUDE.md` says don't write multi-paragraph docstrings or speculative comments. **Comments are reserved for non-obvious why, not what.** This handoff doc is the exception.
