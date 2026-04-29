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
- `pipelines/` — Java/Beam pipeline (Gradle Kotlin DSL). Owns the per-tile fetch, COG transcoding, GCS write, VRT manifest.
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
   → raw GeoTIFF (uncompressed strip layout, single image == tileSize×tileSize)
TileFetchDoFn — fetches and dead-letters on retryable failures
   → FetchedTile (raw bytes + coordinate)
TileWriterDoFn — calls CogTranscoder, then writes to GCS or local disk
   → COG file per tile, named tile_r{row:04d}_c{col:04d}.tif
VrtAssembler — emits a `.vrt` manifest stitching tiles into one virtual raster
```

The output of the pipeline is N tile COGs + 1 VRT, **not** a single mosaic COG. M6 (two-tier tiling) will change that.

### Multi-block COGs (M6)

`CogTranscoder` emits multi-block COGs. The image dimensions must be a whole-number multiple of `tileSize` (the COG's internal block size); partial-edge tiles aren't supported. For one-COG-per-fetch (the default mode), `imageWidth == imageHeight == tileSize` and there's a single block. For M6 two-tier tiling, the assembler builds an `output_tile_size × output_tile_size` buffer (always a multiple of `tileSize`) and `CogTranscoder` partitions it into `(output_tile_size / tileSize)²` inner blocks, each compressed independently per the TIFF spec.

Two entry points:
- `transcode(rawGeotiff, tileSize, compression)` — input is an EE-HV-shaped GeoTIFF; used by the one-COG-per-tile path.
- `transcodeFromAssembledPixels(pixelData, width, height, tileSize, sourceTiffForMetadata, outputTileOriginX, outputTileOriginY, compression)` — input is a pre-assembled pixel buffer plus a representative compute tile for CRS/sample-structure metadata. Used by [`AssembledCogWriter`](../pipelines/src/main/java/com/datensee/io/AssembledCogWriter.java). The source tile's `ModelTiepoint` is overridden with the output tile's origin; everything else (`ModelPixelScale`, `GeoKeyDirectoryTag`, `GeoAsciiParams`, etc.) is inherited.

### Why deflate is the default, not LZW

The hand-rolled LZW encoder in `CogTranscoder.lzwCompress` produces output that strict TIFF-LZW decoders (GDAL, imagecodecs, EE's loader) reject as "Corrupted tile: failed to decompress using scheme LZW". The encoder is kept in tree for future replacement against a canonical test vector but **deflate is the default and the only end-to-end-verified compression**. Don't change the default without first fixing or replacing the LZW encoder. The LZW *decoder* is exercised on input but not pinned by tests; assume it's load-bearing only for inputs the EE HV API actually returns.

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

## Adaptive retry — wire contract is in tree, splitter is not

The failures journal (`{output}/_failures.json`) is now a structured NDJSON of `FailedTileRecord`: a superset of `TileCoordinate` with `error_kind`, `attempts`, timestamps, and `lineage` (a quadtree path from the root compute tile, encoded as a list of integers `0–3` to stay CRS-axis-order-independent). Today it's emitted with placeholder values (`error_kind=UNKNOWN`); a future `datensee retry --journal` command will read it back through the existing `tiles_file` path. The full design — error classification, conservative split allowlist, depth cap, retry semantics — is in [`docs/retry-with-journal.md`](retry-with-journal.md). **The wire format is the contract; please don't change it casually.**

`TileCoordinate.lineage` defaults to `[]` and is informational on the success path: the assembler keys on bounding-box geometry, not lineage. Lineage is for the failure-journal and the (future) retry-decision logic.

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

1. **LZW encoder is broken.** Either fix against a TIFF-LZW canonical vector or rip it out. Default is deflate; LZW only routes if user explicitly sets `compress: "lzw"`.
2. **Adaptive-retry splitter not implemented.** Wire contract is in tree (see [`docs/retry-with-journal.md`](retry-with-journal.md)) — error classification + the `datensee retry --journal` CLI + the splitter logic itself are follow-ups.
3. **`/proc/self/fd/<N>` is Linux-only.** Service-driven auth path won't work on macOS or Windows. Standalone CLI on those OSes uses ADC and is fine.
4. **Predictor is only applied for LZW.** Deflate would also benefit from horizontal differencing for integer types — pure compression-ratio win, no correctness issue.
5. **`extractPixelData` for tile-layout inputs concatenates tile bytes in tile order**, not pixel-row order. Safe today because EE HV always returns strip layout; would silently produce wrong pixels for a multi-tile input.
6. **Partial output tiles are zero-filled.** If some compute tiles in an output group failed and were dead-lettered, the assembler emits a partially-populated COG with zero-filled gaps. Whether to skip the output tile entirely or surface a warning is a design decision left for the next iteration.

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

The COG-specific suite is `pipelines/src/test/java/com/datensee/io/CogTranscoderTest.java`. It uses an independent `TestTiffReader` (deliberately decoupled from `CogTranscoder`'s own decoder) so a bug in the writer can't mask itself by being read back through the same broken assumptions. **Do not collapse the test reader into `CogTranscoder`'s helpers**; the independence is the point.

---

## Conventions worth knowing

- Python: type hints on every signature, Pydantic v2, `pathlib.Path`, `httpx`, Google-style docstrings. Run `ruff format` and `ruff check` before committing.
- Java: records + sealed interfaces + pattern matching. Java 25 source, but `--release 21` for Dataflow workers (don't accidentally use Java 22+ APIs). Google Java Style via Checkstyle.
- Errors are user-facing: say what went wrong AND what to do (e.g. `clip_expression`'s "Reduce the collection first (e.g. .median(), .mosaic(), .first())").
- Conventional Commits.
- `CLAUDE.md` says don't write multi-paragraph docstrings or speculative comments. **Comments are reserved for non-obvious why, not what.** This handoff doc is the exception.
