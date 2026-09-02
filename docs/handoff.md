# Handoff Notes

A reference for an agent picking up DatensEE for polish work. Read after `CLAUDE.md`.

`CLAUDE.md` already covers the high-level architecture, core design decisions, and conventions; do not duplicate it. This file captures **non-obvious decisions, recent fixes, current invariants, and known limitations** that you would otherwise have to spelunk for.

---

## Deployment story (read first)

DatensEE lives at **github.com/michaelfdewitt/datensee** and is published to PyPI as **`datensee`** (release flow: [`releasing.md`](releasing.md)).

The two facts the next agent must internalize:

1. **Users install with `pip install datensee`.** That's the well-lit path. Cloud mode launches a Flex Template pinned to the package version; local mode auto-downloads the matching pipeline JAR from GitHub Releases on first run. No `gradle`, no `uv sync`, no Java installation, no manual JAR placement. The README's "Development setup" section is the only place where Gradle/uv appear, and it's labeled as such.
2. **`datensee` does NOT depend on `earthengine-api`.** The two packages are deliberately decoupled. We accept EE expressions as **opaque serialized JSON** — we never call `ee.serializer.encode` ourselves at runtime. Users who want to author expressions can install `earthengine-api` separately. **Do not add an `earthengine-api` dependency to `cli/pyproject.toml`** — that would balloon the install footprint and pin users to a specific EE client version.

Concretely the dep set is small: typer, pydantic, httpx, rich, pyproj, shapely, google-auth, google-cloud-storage. One optional extra: `[validation]` adds rasterio. Keep it that way.

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
TileWriterDoFn (or AssembledCogWriter in two-tier mode) — calls CogTranscoder,
   writes to GCS or local disk; transcode/assembly/upload exceptions
   dead-letter into _failures.json (error_kind=UNKNOWN, message prefixed
   "write-stage:") instead of failing the job
   → COG file per output tile, named tile_r{row:04d}_c{col:04d}.tif
     (compute-tile indices in one-COG-per-tile mode; out_row/out_col
     indices in two-tier mode)
```

The output of the pipeline is a directory of COG files, one per output tile. There is no manifest file (no VRT, no JSON sidecar, no XML) — output COGs are self-describing via standard GeoTIFF tags, and any modern GIS tool (QGIS, rasterio, ArcGIS) opens a directory of geotagged TIFFs as a layer set without help. Users who want a single stitched COG control granularity via `output_tile_size_pixels`: set it large enough to cover the export region and the pipeline emits one COG; leave it small (or unset) and the pipeline emits many.

### Multi-block COGs (two-tier)

`CogTranscoder` emits multi-block COGs. The image dimensions must be a whole-number multiple of `tileSize` (the COG's internal block size); partial-edge tiles aren't supported. For one-COG-per-fetch (the default mode), `imageWidth == imageHeight == tileSize` and there's a single block. For two-tier tiling, each compute tile becomes one inner COG block: the assembler extracts pixels per compute tile, places them by `(tx, ty)` block index in a row-major `List<byte[]>` (null entries are zero-filled), and `CogTranscoder` compresses each block independently per the TIFF spec — no intermediate `output_tile_size × output_tile_size` buffer.

Three entry points:
- `transcode(rawGeotiff, tileSize, compression)` — input is an EE-HV-shaped GeoTIFF; used by the one-COG-per-tile path.
- `transcodeFromPixelBuffer(pixels, width, height, tileSize, sourceTiffForMetadata, outputAffine, compression)` — input is the fully-assembled row-major pixel canvas of the output image; the transcoder slices it into blocks itself. This is what [`AssembledCogWriter`](../pipelines/src/main/java/com/datensee/pixel/io/AssembledCogWriter.java) uses: the assembler composes baseline pixels (from a previously-written COG, when merging a retry round) plus freshly-fetched tiles — including sub-block quadtree split children — onto one canvas, so placement never leaks into the transcoder.
- `transcodeFromTileBlocks(tilePixels, width, height, tileSize, sourceTiffForMetadata, outputAffine, compression)` — row-major list of per-block buffers (`null` → zero-filled); the layer `transcodeFromPixelBuffer` sits on.

In both multi-block paths the source tile's georeferencing is overridden from the output affine (`ModelTiepoint` + `ModelPixelScale`); everything else (`GeoKeyDirectoryTag`, `GeoAsciiParams`, sample-structure tags) is inherited from a representative compute tile. Overflow tag data is padded to even offsets (TIFF 6.0 word alignment).

**The two-tier origin contract (load-bearing):** `decompose_region` snaps the parent-grid origin to *output*-tile boundaries whenever `output_tile_size_pixels` is set, and assigns `out_row`/`out_col = (row_px // OTS, col_px // OTS)` in local coordinates. The assembler derives every group's origin *from its key* (`outCol·OTS`, `outRow·OTS`) — independent of GroupByKey iteration order. Tiles outside their key's rect indicate a broken tiler and dead-letter the group. Don't change either side without the other.

**Retry merge:** `output.merge_existing_output` (set by `datensee retry`, never by fresh exports) makes the assembler decode the existing output COG as the baseline canvas before overlaying this round's tiles. Without it a retry round would rebuild the COG from only the re-fetched tiles, zero-filling every previously-good block. Fresh exports keep replace semantics so stale files are never silently blended into. Partial *fresh* writes are zero-filled, WARN-logged, and counted on `output_tiles_partial`; merges bump `output_tiles_merged`. There is **no per-file `.partial.json` sidecar** — `_failures.json` is the canonical record of missing data.

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

Tests pin the layout (`transcodedFileHasFirstIfdAtOffsetEight`, `tileOffsetsPointPastTheIfdIntoPixelData`, `outputDeclaresTileLayoutAndDropsStripTags`) and pixel-data round-trips (uint8/uint16/int16/float32, multi-band, deflate input, GeoTIFF metadata preservation). See `pipelines/src/test/java/com/datensee/pixel/io/CogTranscoderTest.java`.

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

July 2026 review round (this branch) — the external correctness review found and fixed, in one sweep:

| Area | What was wrong → what changed |
| --- | --- |
| two-tier origin seam | Python assigned `out_row`/`out_col` from global output-tile indices while the Java assembler snapped *local* offsets — crashes or overlapping COGs whenever the export origin wasn't output-aligned. Fixed by snapping the parent-grid origin to output-tile boundaries and deriving out indices (and the assembler's origin) from pure local arithmetic. |
| two-tier retry data loss | A retry round rebuilt each touched output COG from only the re-fetched tiles, zero-filling every previously-good block; split children couldn't pass the transcoder's block-size check at all. Fixed via `merge_existing_output` + canvas assembly with sub-block placement. |
| Write-stage failures | Transcode/assembly/upload exceptions failed the whole job; now they dead-letter into `_failures.json` symmetric with fetch failures. |
| Error classification | 504 / body-matched 5xx classified as `COMPUTATION_TIMEOUT` (split-eligible) — infra storms would have cascaded 4×. Split signatures now gated to HTTP 400. |
| Local-mode hang | `_run_local_with_progress` never drained the JVM's pipes; exports deadlocked once ~64KB of Beam logs accumulated. A reader thread now drains into a bounded tail. |
| Watchdog | Idle detection used `max()` across unrelated counters and cancelled healthy jobs during slow assembly; `max_runtime` measured the poll session, not the job; the bearer token was never refreshed; `DRAINED`/`UPDATED` never terminated the loop. All fixed in `status.py`. |
| Validation suite | Knew nothing about two-tier (false FAILs on E01/E08, false PASSes on E02/E03 at zero-checked); E07 broke on EE's structured NPY responses. Rewritten around "output units" (`pixel/validation/units.py`). |
| Service wrapper | `/submit` 500'd after submission (removed `scale_meters` attribute); `/submit-task` had no atomic claim and inverted transient/permanent retry semantics. |
| Retry preconditions | `_export_meta.json` (with `pixel_grid`) is now a hard requirement; snapshot pinning rejects `<= 0` (EE's `-1` "latest" sentinel) and warns on unpinnable `loadGeoTIFF` nodes. |
| Config surface | `CogParameters` reduced to `{compress}` — overviews/blocksize/predictor were never implemented; the schema and shipped example config were stale (`lzw`); a contract test (`cli/tests/test_contract_schema.py`) now pins model↔schema parity. |

Follow-up round (same review, next day):

| Area | What changed |
| --- | --- |
| Pipeline-side carryover merge | Retry stages `_carryover.json`; the envelope's `carryover_file` points the pipeline at it; `DatensEEPipeline.writeFailuresJournal` unions it with fresh failures — journal complete on every runner, no Python post-step. `gs://` journals read directly. |
| Retry loop driver | `datensee retry --until-done` / `api.retry_until_done` — rounds with backoff, Dataflow polling between rounds, green/stuck/failed exit reporting. |
| Splits everywhere | Every retry round routes through the assembler's merge path (`merge_existing_output`), so quadtree splits are legal for non-two-tier exports too; `split_disabled` removed. Fresh non-two-tier exports keep the direct no-shuffle writer. |
| Nodata | `output.nodata` → `GDAL_NODATA` tag (42113) on every COG, persisted in `_export_meta.json` and inherited on retry. EE returns masked pixels as 0; users `unmask(sentinel)` + `--nodata`. |
| Cost estimate | `datensee.cost.estimate_cost` + a cost section in the pre-submit panel (EECU range, Dataflow USD, two-tier shuffle line, storage $/mo) — constants dated, assumptions listed. |
| Integration coverage | `TestTwoTierExportRetryMergeEndToEnd` runs the real pipeline through export → validate → retry (retry-same + split + terminal carryover) → merge + journal-union + nodata assertions. |

---

Remote e2e round (September 2026, bare Debian LXC with only ADC — full log in [`remote-e2e-log-2026-09-01.md`](remote-e2e-log-2026-09-01.md)):

| Area | What was wrong → what changed |
| --- | --- |
| GCS client project | `storage.Client()` inferred the project from gcloud's `core/project`; a pip-only host (ADC, no gcloud) crashed before launch with "Project was not passed". `auth.gcs_client` resolves explicit → quota project → explicit `None`, and every GCS site uses it. |
| Flex Template freshness | The published `0.1.0a1` launcher JAR predated the `pipeline_kind` envelope and failed inside the launcher (strict unknown-field rejection did its job). `v0.1.0a2` was re-staged. The worker harness is `beam-java21-batch`, so the JAR must stay Java 21 bytecode (`options.release = 21`). `scripts/release_template_cloudbuild.py` stages a release with Cloud Build + ADC only (no Docker, no gcloud). |
| Failure visibility | `status` reported `JOB_STATE_FAILED` with no reason; launcher stack traces live in `<staging>/template_launches/<job>/console_logs`, not Cloud Logging. `status.failure_summary` prints the distinct ERROR job messages and that GCS path. `export` prints the `datensee status …` command after a Dataflow submit. |
| Fan-out | The tile source (in-memory `Create` or one NDJSON file) is single-shard and Dataflow fused the fetch ParDo into it ("Parallelism will be set to 1"). `Reshuffle.viaRandomKey()` after the source. |
| `validate gs://…` | `Path("gs://…")` made every unit "file missing". The prefix's COGs + journal are staged into a temp dir and the local checks run unchanged. |
| Worker sizing | `--machine-type` / `--num-workers` / `--max-workers` exposed on the CLI (a `ZONE_RESOURCE_POOL_EXHAUSTED` stockout on n2-standard-4 needed `e2-standard-4`). |

## Adaptive retry — implemented

The failures journal (`{output}/_failures.json`) is structured NDJSON of `FailedTileRecord` (superset of `TileCoordinate` with `error_kind`, `attempts`, timestamps, `lineage`). It carries **both** fetch failures and write-stage failures (transcode/assembly/upload, `error_kind=UNKNOWN`, message prefixed `write-stage:`). `EeErrorKind.classify(httpStatus, body)` (Java) populates the kind from the EE HV response — split-eligible signatures are gated to HTTP 400; the dead-letter side output is typed `FailedTileRecord`. `datensee retry --journal _failures.json` reads it back, splits split-eligible failures into 4 quadrant children (lineage extended with quadrant index 0–3, CRS-axis-order-independent), retries transient failures with the same bbox, and submits a fresh pipeline run via `tile_grid.tiles_file`. Default max depth is 2 (one root → 16 sub-tiles max).

**Conservative split allowlist:** only `MEMORY_EXCEEDED` and `COMPUTATION_TIMEOUT`. Generic 5xx, rate-limit, auth errors never trigger splitting — that's intentional and pinned in tests. Adding a kind is a one-line config change later; removing one that's already triggering production cascades is a fire.

`TileCoordinate.lineage` defaults to `[]` and is informational on the success path — the assembler keys on bbox geometry, not lineage. Lineage is consumed by the retry-decision logic and shows up in the journal for human triage.

Full design + retry semantics in [`docs/retry-with-journal.md`](retry-with-journal.md).

---

## Two-tier tiling — wiring summary

Compute tiles flow into the pipeline as before. When `output.output_tile_size_pixels` is set on `OutputConfig`, `PixelOutputTransform` swaps the per-tile writer (`CogWriter`) for `AssembledCogWriter`, which:

1. Keys each `FetchedTile` by `(out_row, out_col)` — assigned in `tiling.decompose_region` as `(row_px // OTS, col_px // OTS)` in local parent-grid coordinates. The parent-grid origin is snapped to *output*-tile boundaries in two-tier mode, so this arithmetic and the assembler's key-derived origin agree by construction.
2. `GroupByKey` shuffles compute tiles together by output tile (deterministic `OutputTileKeyCoder`).
3. `AssembleAndWriteDoFn` builds a pixel canvas for the output tile — starting from the decoded existing COG when `merge_existing_output` is set (retry rounds), from zeros otherwise — overlays each fetched tile's pixels at its integer offset (sub-block split children included), and calls `CogTranscoder.transcodeFromPixelBuffer`.
4. The resulting multi-block COG is written as `tile_r{out_row:04d}_c{out_col:04d}.tif`. Assembly/write exceptions dead-letter the group's tiles into `_failures.json` rather than failing the job.

The internal block size of the output COG is the compute tile size, so EE's `loadGeoTIFF` can random-access individual compute-tile-sized regions efficiently.

When `output_tile_size_pixels` is unset (the default), routing falls through to the existing one-COG-per-tile path with no behavior change.

---

## Known limitations / TODOs in priority order

1. **Geographic-CRS pixel size uses the equator constant (`1/111_320` deg/m) by design.** [`tiling._pixel_size_native`](../cli/src/datensee/pixel/tiling.py) converts `scale_meters` to degrees with no latitude correction, so `scale_meters` is a nominal label for geographic CRSs (true ~30 m at the equator, ~15 m ground distance at lat 60°). The payoff is unconditional cross-export grid alignment. Users who need true metric pixels supply a projected CRS.
2. ~~No automatic retry-loop driver.~~ **Resolved:** `datensee retry --until-done` (backed by `api.retry_until_done`) loops rounds — with backoff between them, and polling Dataflow jobs to terminal state — until the journal has no retryable work or `--max-rounds` is hit.
3. ~~Dataflow / GCS carryover merge isn't wired yet.~~ **Resolved (pipeline-side carryover merge):** the retry CLI stages `{output}/_carryover.json`, the config's `carryover_file` field points the pipeline at it, and `DatensEEPipeline.writeFailuresJournal` unions those lines with the round's fresh failures before writing `_failures.json` — identical path local + Dataflow, no Python post-step. `datensee retry --until-done` drives rounds to completion (polling Dataflow jobs between rounds), and `gs://` journals are read directly.
4. **`/proc/self/fd/<N>` is Linux-only.** Service-driven auth path won't work on macOS or Windows. Standalone CLI on those OSes uses ADC and is fine.
5. **Dataflow job-id extraction uses reflection against a deprecated API.** [`DatensEEPipeline.run`](../pipelines/src/main/java/com/datensee/DatensEEPipeline.java) calls `getJobId` on the result via reflection so we don't pull `beam-runners-google-cloud-dataflow-java` into the compile classpath of every consumer; the current Beam version emits a deprecation warning at compile time. Two ways out: (a) add the runtime dep at compile time and call `((DataflowPipelineJob) result).getJobId()` directly, accepting the dep bloat, or (b) read the job id from the runner's stdout/stderr ahead of `waitUntilFinish` — already partially done via the `DATENSEE_JOB_ID=` echo. Pick one when the Beam minor version next bumps and the deprecation graduates to removal.
6. ~~`extractPixelData` for tile-layout inputs concatenates tile bytes in tile order, not pixel-row order. Safe today because EE HV always returns strip layout; would silently produce wrong pixels for a multi-tile input.~~ **Both halves of this comment turned out to be wrong.** EE HV does *not* return strip layout — it returns tile layout (256×256 internal tiles, deflate-compressed) for every request. And the failure mode wasn't "silently wrong pixels": each tile is its own zlib stream, so feeding the concatenation into a single `Inflater` stopped at the first end-of-stream marker, dropped every tile after the first, and crashed `extractBlock` downstream with `ArrayIndexOutOfBoundsException`. Fixed: `extractPixelData` now decompresses each chunk independently and assembles tile-layout inputs into row-major order. Pinned by `CogTranscoderTest#multiTileDeflateInputRoundTripsCorrectly` (synthesizes EE's actual response shape: 4-tile 2×2 grid of 256×256 deflate float32) and documented by `cli/scripts/probe_hv_dimensions.py`.
7. ~~Partial output tiles are zero-filled silently.~~ **Resolved:** fresh partial writes are still zero-filled at the gaps (partial data beats no data) but are WARN-logged and counted on `output_tiles_partial`, the missing tiles are in `_failures.json`, and a `datensee retry` round fills the holes in place via `merge_existing_output`.
8. **Predictor encoding is not applied for any compression.** Horizontal differencing on integer-typed bands would give a free compression-ratio win for deflate; we don't do it today. Pure ratio question, no correctness issue. (`output.compression` is deliberately the only COG knob — the config surface only carries what the pipeline implements.)

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

The COG-specific suites are `pipelines/src/test/java/com/datensee/pixel/io/CogTranscoderTest.java` (single-tile transcode round-trips), `CogTranscoderAlignmentTest.java` (TIFF word-alignment of overflow tag data), and `AssembledCogWriterTest.java` (two-tier multi-block assembly, the 256-tile pixel-perfect validation case, key-derived origins for non-zero output keys, sub-block split-child placement, retry merge, and write-stage dead-lettering). Both decode COG output via `TestTiffReader` ([source](../pipelines/src/test/java/com/datensee/pixel/io/TestTiffReader.java)) — a separate test-only reader deliberately decoupled from `CogTranscoder`'s own decoder paths so a bug in the writer can't mask itself by being read back through the same broken assumptions. **Do not collapse the test reader into `CogTranscoder`'s helpers**; the independence is the point.

---

## Conventions worth knowing

- Python: type hints on every signature, Pydantic v2, `pathlib.Path`, `httpx`, Google-style docstrings. Run `ruff format` and `ruff check` before committing.
- Java: records + sealed interfaces + pattern matching. Java 25 source, but `--release 21` for Dataflow workers (don't accidentally use Java 22+ APIs). Google Java Style via Checkstyle.
- Errors are user-facing: say what went wrong AND what to do (e.g. the ImageCollection rejection's "Reduce the collection first (e.g. .median(), .mosaic(), .first())").
- Conventional Commits.
- `CLAUDE.md` says don't write multi-paragraph docstrings or speculative comments. **Comments are reserved for non-obvious why, not what.** This handoff doc is the exception.
