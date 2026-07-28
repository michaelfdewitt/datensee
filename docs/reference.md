# DatensEE Reference

A single-page reference for the DatensEE CLI, Python API, pipeline
config schema, and operational concerns. For the *why*, see
[blog.md](../blog.md). For deployment notes and recent fixes, see
[handoff.md](handoff.md).

## Contents

- [Install](#install)
- [Quickstart](#quickstart)
- [CLI reference](#cli-reference)
- [Python API reference](#python-api-reference)
- [Pipeline config](#pipeline-config)
- [Costs and quotas](#costs-and-quotas)
- [Troubleshooting](#troubleshooting)

## Install

```
pip install datensee
```

Requires Python 3.12+.

Optional dependency groups:

| Extra          | Adds                  | When you need it                                          |
| -------------- | --------------------- | --------------------------------------------------------- |
| `validation`   | `rasterio`            | `datensee validate` post-export integrity checks          |

Example: `pip install 'datensee[validation]'`.

DatensEE also needs the Java pipeline JAR. After installing the Python
package, fetch it once:

```
datensee jar download         # prebuilt from GitHub Releases (default)
datensee jar build            # build from source (requires JDK 25, Gradle)
datensee jar path             # print the resolved path
```

`export` and `demo` auto-detect the JAR from a known location; pass
`--jar PATH` to override.

## Quickstart

### CLI — local demo

A self-contained Landsat 9 NDVI export over the SF Bay Area, run on the
in-process Beam direct runner. No GCS, no Dataflow.

```
datensee demo --project YOUR_GCP_PROJECT
```

Outputs land in `./datensee-output/` as a directory of COG files,
named `tile_r{row:04d}_c{col:04d}.tif`. Open the directory in QGIS,
rasterio, or any GIS tool — geotagged TIFFs are self-describing.
Use `--output-tile-size` (two-tier tiling) to control how many
output COGs you get.

### CLI — your own export

```
datensee export expression.json region.geojson \
    --project YOUR_GCP_PROJECT \
    --output gs://your-bucket/run-2026-04-29/ \
    --temp-location gs://your-bucket/dataflow-tmp/ \
    --runner dataflow \
    --scale 30 \
    --crs EPSG:4326
```

`expression.json` is the output of `ee.serializer.encode(your_image)`.
`region.geojson` is a Polygon or MultiPolygon (WGS84).

### Python

```python
import json
import datensee

result = datensee.export(
    ee_expression=open("expression.json").read(),
    region=json.load(open("region.geojson")),
    project="YOUR_GCP_PROJECT",
    output="gs://your-bucket/run-2026-04-29/",
    temp_location="gs://your-bucket/dataflow-tmp/",
    scale=30,
    crs="EPSG:4326",
    runner="dataflow",
)

print(result.job_id)
datensee.poll(result.job_id, project="YOUR_GCP_PROJECT")
```

In a notebook, `datensee.notebook.display_job_progress(result.job_id, ...)`
gives you an HTML progress view instead of Rich.

## CLI reference

Top-level options:

| Flag             | Effect                         |
| ---------------- | ------------------------------ |
| `--version`, `-V`| Print the installed version    |

Subcommands: [`demo`](#datensee-demo), [`export`](#datensee-export),
[`status`](#datensee-status), [`retry`](#datensee-retry),
[`validate`](#datensee-validate), [`jar`](#datensee-jar).

### `datensee demo`

Run the bundled Landsat 9 NDVI demo locally.

| Flag             | Type   | Default               | Description                                                              |
| ---------------- | ------ | --------------------- | ------------------------------------------------------------------------ |
| `--project`, `-p`| str    | *required*            | GCP project ID with the Earth Engine API enabled                         |
| `--output`, `-o` | path   | `./datensee-output`   | Local directory for output COG tiles. Created if absent.                 |
| `--jar`          | path   | auto-detected         | Path to the pipeline JAR. Override only when running an unreleased build |
| `--dry-run`      | flag   | off                   | Print the pipeline command without executing                             |

### `datensee export`

Submit an export job. Two positional arguments: an expression file and a
region file.

Positional:

| Argument          | Description                                                                |
| ----------------- | -------------------------------------------------------------------------- |
| `EXPRESSION_FILE` | JSON file containing a serialized EE computation (`ee.serializer.encode`)  |
| `REGION_FILE`     | GeoJSON file with a Polygon or MultiPolygon (WGS84)                        |

Options:

| Flag                          | Type   | Default        | Description                                                                          |
| ----------------------------- | ------ | -------------- | ------------------------------------------------------------------------------------ |
| `--project`, `-p`             | str    | *required*     | GCP project with the EE API enabled                                                  |
| `--output`, `-o`              | str    | *required*     | GCS URI (`gs://…`) for Dataflow, or a local directory for `--runner local`           |
| `--scale`, `-s`               | float  | `30.0`         | Pixel size in meters (must be > 0)                                                   |
| `--crs`                       | str    | `EPSG:4326`    | Target CRS — EPSG code or proj string                                                |
| `--tile-size`                 | int    | `512`          | Compute tile edge length in pixels (size of each HV API request)                     |
| `--output-tile-size`          | int    | unset          | two-tier tiling — output COG edge in pixels, must be a positive multiple of `--tile-size`. Unset = one COG per compute tile |
| `--nodata`                    | float  | unset          | Nodata value stamped on every output COG as the `GDAL_NODATA` tag. EE returns masked pixels as 0 — `unmask(sentinel)` your expression and pass the sentinel here so GIS tools can tell nodata from real zeros |
| `--runner`                    | str    | `local`        | `local` (in-process Beam) or `dataflow`                                              |
| `--region-gcp`                | str    | `us-central1`  | Dataflow region                                                                      |
| `--temp-location`             | str    | unset          | GCS URI for Dataflow temp/staging files. Required when `--runner dataflow`           |
| `--jar`                       | path   | auto-detected  | Path to the pipeline JAR                                                             |
| `--snapshot-time`             | str    | submit time    | Pin every asset reference in the EE expression to this moment (ISO-8601 UTC or Unix microseconds). Override only to reproduce a prior export |
| `--dry-run`                   | flag   | off            | Print the pipeline command without executing                                         |
| `--yes`, `-y`                 | flag   | off            | Skip the confirmation prompt for jobs > 10 000 tiles                                 |
| `--validate / --no-validate`  | flag   | `--no-validate`| Run zero-cost output checks after the pipeline completes (local mode only)          |

Note: the CLI default for `--runner` is `local`; the Python API default
is `dataflow`. The CLI bias toward local matches a "try it, then scale
it" workflow; the API bias toward Dataflow matches programmatic /
service-side use.

### `datensee status`

Poll a Dataflow job to a terminal state (`DONE`, `FAILED`, `CANCELLED`,
`DRAINED`, or `UPDATED`), routed through `api.poll`. Applies cost-control
watchdog policies by default: a wall-clock cap on **job age** (anchored to
the Dataflow `createTime`, so reattaching to an old job doesn't reset the
clock), a failure-rate circuit breaker, and per-counter idle detection.
Any breach cancels the underlying job.

| Argument / Flag           | Type   | Default       | Description                                                                    |
| ------------------------- | ------ | ------------- | ------------------------------------------------------------------------------ |
| `JOB_ID` (positional)     | str    | *required*    | Dataflow job ID                                                                |
| `--project`               | str    | *required*    | GCP project ID                                                                 |
| `--region-gcp`            | str    | `us-central1` | Dataflow region                                                                |
| `--tile-count`            | int    | unset         | Total compute tile count; enables the failure-rate check                       |
| `--max-runtime-hours`     | float  | `12`          | Hard cap on job age (createTime-anchored). `<= 0` disables                     |
| `--max-failure-rate`      | float  | `0.5`         | Cancel if failures / tile_count exceeds this after the grace period. `<= 0` disables |
| `--failure-grace-minutes` | float  | `10`          | Skip the failure-rate check while the worker pool ramps up                     |
| `--idle-timeout-minutes`  | float  | `20`          | Cancel if no counter progress for this long. `<= 0` disables                   |
| `--no-watchdog`           | flag   | off           | Disable all watchdog checks at once                                            |

Exits 0 on `JOB_STATE_DONE`, 2 when a watchdog cancels the job,
non-zero otherwise.

### `datensee retry`

Re-submit failed tiles from a failures journal, splitting EE complexity
failures (`MEMORY_EXCEEDED`, `COMPUTATION_TIMEOUT`) into 4 quadtree
children and retrying transient failures as-is. Requires the
`{output}/_export_meta.json` sidecar written by the original export
(hard error without it) — most flags below default to the values
persisted there. See [retry-with-journal.md](retry-with-journal.md).

| Flag                 | Type  | Default                    | Description                                                        |
| -------------------- | ----- | -------------------------- | ------------------------------------------------------------------ |
| `--output`, `-o`     | str   | *required*                 | Output path of the original export (anchors meta + journal)        |
| `--journal`, `-j`    | path  | `{output}/_failures.json`  | Failures journal (NDJSON)                                          |
| `--expression`, `-e` | path  | from meta                  | Serialized EE expression file                                      |
| `--project`, `-p`    | str   | from meta                  | GCP project ID                                                     |
| `--scale`, `-s`      | float | from meta                  | Pixel size in meters                                               |
| `--crs`              | str   | from meta                  | Target CRS                                                         |
| `--tile-size`        | int   | from meta                  | Compute tile edge in pixels                                        |
| `--output-tile-size` | int   | from meta                  | two-tier output tile size                                                |
| `--max-depth`        | int   | `2` (max 6)                | Quadtree depth cap; deeper tiles stay in the journal               |
| `--until-done`       | flag  | off                        | Loop retry rounds until the journal has no retryable work; Dataflow jobs are polled to completion between rounds |
| `--max-rounds`       | int   | `5` (max 25)               | Round budget for `--until-done`                                    |
| `--round-backoff`    | float | `30.0`                     | Seconds between `--until-done` rounds (lets EE transients clear)   |
| `--runner`, `--region-gcp`, `--temp-location`, `--jar`, `--dry-run` | | | As for `export` |

The journal may be a local path or a `gs://` URI; for GCS outputs it
defaults to `{output}/_failures.json` on GCS. Every retry round runs with
`merge_existing_output`: re-fetched tiles — including quadtree split
children — overlay the existing output COGs in place, and records that
made no progress (terminal kinds, depth-capped splits) are staged to
`{output}/_carryover.json` and unioned into the new journal by the
pipeline itself.

### `datensee validate`

Run the post-export check suite (E01–E04, E07–E10 — E05/E06 are retired
VRT checks, see [validation.md](validation.md)) against an output
directory or GCS prefix. Requires the `validation` extra
(`pip install 'datensee[validation]'`).

| Argument / Flag       | Type   | Default                  | Description                                                                         |
| --------------------- | ------ | ------------------------ | ----------------------------------------------------------------------------------- |
| `OUTPUT_PATH`         | str    | *required*               | Output directory (local) or `gs://` prefix to validate                              |
| `--config`, `-c`      | path   | *required*               | Pipeline config JSON file that produced the output                                  |
| `--checks`, `-e`      | str    | all zero-cost            | Comma-separated check IDs (e.g. `E01,E03,E07`). Overrides `--reference`             |
| `--sample`            | int    | `20`                     | Tile sample size for sampling-based checks                                          |
| `--reference`         | flag   | off                      | Run E07 (per-pixel comparison vs. EE HV API). Costs EECUs                           |
| `--gee-project`       | str    | from config              | GCP project for E07 reference fetches. Defaults to the config's `gee_project`       |
| `--json`              | path   | unset                    | Write a machine-readable JSON report to this file                                   |

See [validation.md](validation.md) for individual check descriptions.

### `datensee jar`

Manage the pipeline JAR.

| Subcommand | Flags                          | Description                                                       |
| ---------- | ------------------------------ | ----------------------------------------------------------------- |
| `path`     | —                              | Print the resolved JAR path or exit non-zero if not found         |
| `download` | `--version`, `-v` (str, default = installed version) | Download a prebuilt JAR from GitHub Releases       |
| `build`    | —                              | Build the JAR from source (requires JDK 25 and Gradle)            |

## Python API reference

Public surface, all importable from the top-level `datensee` package:

| Symbol                     | What it is                                                          |
| -------------------------- | ------------------------------------------------------------------- |
| `datensee.export`          | Submit an export job (Dataflow or local)                            |
| `datensee.demo`            | Run the bundled NDVI demo locally                                   |
| `datensee.tile`            | Decompose a region into a tile grid (no submission)                 |
| `datensee.poll`            | Poll a Dataflow job to a terminal state                             |
| `datensee.ExportResult`    | Pydantic result model returned by `export()` and `demo()`           |

`datensee.api.retry(...)` (not re-exported at the top level) is the
programmatic equivalent of `datensee retry` — see
[retry-with-journal.md](retry-with-journal.md).

### `datensee.export(...)`

Validates inputs, tiles the region, builds the pipeline config, and
submits the job. For Dataflow mode, returns immediately after submission
with a `job_id`. For local mode, blocks until the pipeline completes.

| Param               | Type                                | Default       | Description                                                                                                                                    |
| ------------------- | ----------------------------------- | ------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| `ee_expression`     | `ee.Image \| str`                   | *required*    | A live `ee.Image` (serialized with your installed earthengine-api client — datensee never imports `ee`), or a pre-serialized cloud-API expression JSON string |
| `region`            | `dict \| ee.Geometry \| shapely`    | *required*    | GeoJSON Polygon/MultiPolygon dict (WGS84), an ee.Geometry/Feature/FeatureCollection, or anything with `__geo_interface__` (shapely)           |
| `project`           | `str`                               | *required*    | GCP project with the EE API enabled                                                                                                            |
| `output`            | `str`                               | *required*    | GCS URI (`gs://…`) for Dataflow, or a local directory for `runner="local"`                                                                     |
| `scale`             | `float`                             | `30.0`        | Pixel size in meters                                                                                                                           |
| `crs`               | `str`                               | `"EPSG:4326"` | Target CRS — EPSG code or proj string                                                                                                          |
| `tile_size`         | `int`                               | `512`         | Compute tile edge in pixels (one HV API request per compute tile)                                                                              |
| `output_tile_size`  | `int \| None`                       | `None`        | two-tier tiling. When set, must be a positive multiple of `tile_size`. `None` = one COG per compute tile                                    |
| `runner`            | `Literal["local", "dataflow"]`      | `"dataflow"`  | Runner mode                                                                                                                                    |
| `region_gcp`        | `str`                               | `"us-central1"` | Dataflow region                                                                                                                              |
| `temp_location`     | `str \| None`                       | `None`        | GCS URI for Dataflow temp/staging files. Required when `runner="dataflow"`                                                                     |
| `labels`            | `dict[str, str] \| None`            | `None`        | Dataflow job labels, forwarded as `--labels=JSON`. Useful for filtering `jobs.list` queries downstream. Only applied in `dataflow` mode        |
| `machine_type`      | `str \| None`                       | `None`        | Dataflow worker machine type (default `n2-standard-4`)                                                                                         |
| `num_workers`       | `int \| None`                       | `None`        | Initial Dataflow worker count (default 4)                                                                                                      |
| `max_workers`       | `int \| None`                       | `None`        | Dataflow autoscaling ceiling (default 100)                                                                                                     |
| `autoscaling_algorithm` | `Literal["THROUGHPUT_BASED", "NONE"] \| None` | `None` | Dataflow autoscaling mode (default `THROUGHPUT_BASED`)                                                                              |
| `number_of_worker_harness_threads` | `int \| None`        | `None`        | Per-worker fetcher concurrency (default 8 — above vCPU count because fetches are I/O-bound)                                                    |
| `jar`               | `Path \| str \| None`               | `None`        | Path to the pipeline JAR. Auto-detected when `None`                                                                                            |
| `snapshot_time`     | `int \| None`                       | `None`        | Unix microseconds to pin asset versions to. Defaults to submit time; values `> 1e17` (nanoseconds) or `<= 0` are rejected                      |
| `dry_run`           | `bool`                              | `False`       | Validate and build the config but don't submit                                                                                                 |
| `progress_callback` | `Callable[[int, int], None] \| None`| `None`        | Local-mode progress callback `(completed, total)`. Ignored for Dataflow                                                                        |
| `confirm_callback`  | `Callable[[PipelineConfig], None] \| None` | `None` | Called before submitting large jobs; raise to abort                                                                                            |
| `credentials`       | `google.auth.credentials.Credentials \| None` | `None` | Caller-supplied credentials. When set, skips the ADC bootstrap and uses these credentials for every Google API call. See [auth note](#service-side-auth-credentials) |

Returns: [`ExportResult`](#datenseexpresult).

Raises:

- `ValueError` — input validation failure (bad GeoJSON, unrecognized CRS, missing `temp_location` in Dataflow mode, non-GCS output in Dataflow mode, etc.)
- `FileNotFoundError` — pipeline JAR cannot be located

#### Service-side auth: `credentials`

When DatensEE runs inside a service that holds an end-user's OAuth
token, pass that token's `Credentials` object via `credentials=`.
DatensEE then uses it for the GCS upload on the driver and for the
Dataflow `createJob` call (the Java worker reads it through a
short-lived env var). This bypasses ADC entirely so the driver never
silently falls back to the host service account.

### `datensee.demo(...)`

The bundled NDVI demo, equivalent to `datensee demo` on the CLI.

| Param               | Type                                 | Default               | Description                                            |
| ------------------- | ------------------------------------ | --------------------- | ------------------------------------------------------ |
| `project`           | `str`                                | *required*            | GCP project with the EE API enabled                    |
| `output`            | `str`                                | `"./datensee-output"` | Local directory for output COG tiles                   |
| `jar`               | `Path \| str \| None`                | `None`                | Path to the pipeline JAR (auto-detected when `None`)   |
| `dry_run`           | `bool`                               | `False`               | Validate but don't submit                              |
| `progress_callback` | `Callable[[int, int], None] \| None` | `None`                | Progress callback `(completed, total)`                 |

Returns: [`ExportResult`](#datenseexpresult).

### `datensee.tile(...)`

Decompose a region into a tile grid without submitting anything. Useful
for cost estimation and notebook visualization.

| Param       | Type    | Default       | Description                                                  |
| ----------- | ------- | ------------- | ------------------------------------------------------------ |
| `region`    | `dict \| ee.Geometry \| shapely` | *required* | As for `export`                             |
| `scale`     | `float` | `30.0`        | Pixel size in meters                                         |
| `crs`       | `str`   | `"EPSG:4326"` | Target CRS                                                   |
| `tile_size` | `int`   | `512`         | Tile edge size in pixels                                     |

Returns: `TileGrid` (see [Pipeline config](#pipeline-config)).

### `datensee.poll(...)`

Poll a Dataflow job until it reaches a terminal state (`DONE`, `FAILED`,
`CANCELLED`, `DRAINED`, `UPDATED`). The underlying `status.poll_job`
takes full `Credentials` (not a bare token) and refreshes them on every
tick, so multi-hour polls survive the ~1 h access-token lifetime.

| Param           | Type                                | Default         | Description                                                                                  |
| --------------- | ----------------------------------- | --------------- | -------------------------------------------------------------------------------------------- |
| `job_id`        | `str`                               | *required*      | Dataflow job ID                                                                              |
| `project`       | `str`                               | *required*      | GCP project ID                                                                               |
| `region`        | `str`                               | `"us-central1"` | Dataflow region                                                                              |
| `callback`      | `Callable[[JobInfo], None] \| None` | `None`          | Per-tick callback. When `None`, uses the Rich Live display (CLI mode)                        |
| `poll_interval` | `int`                               | `15`            | Seconds between polls                                                                        |
| `watchdog`      | `WatchdogConfig \| None`            | defaults        | Cost-control circuit breakers: `max_runtime` (12 h, measured against **job age** from Dataflow `createTime`), `max_failure_rate` (0.5), `idle_timeout` (20 min, per-counter progress) |
| `tile_count`    | `int \| None`                       | `None`          | Total compute tile count; enables the failure-rate breaker                                  |

Returns: final `JobState`. Raises `WatchdogTriggered` when a watchdog
policy cancels the job.

### `datensee.ExportResult`

```python
class ExportResult(BaseModel):
    config: PipelineConfig
    job_id: str | None = None
    duration_seconds: float | None = None
    tiles_ok: int | None = None
    tiles_failed: int | None = None
    output_bytes: int | None = None
```

| Field              | Populated when                                                             |
| ------------------ | -------------------------------------------------------------------------- |
| `config`           | Always                                                                     |
| `job_id`           | After submission (Dataflow) or after the local pipeline completes          |
| `duration_seconds` | After submission / completion                                              |
| `tiles_ok`         | Local mode only, post-completion                                           |
| `tiles_failed`     | Local mode only, post-completion                                           |
| `output_bytes`     | Reserved for future use                                                    |

## Pipeline config

The JSON contract handed from Python to the Java Beam pipeline. Defined
by `PipelineConfig` and matched against
[`pipeline-config.schema.json`](../contract/pipeline-config.schema.json).
You only need to read this directly if you're driving the Java pipeline
yourself or writing a third-party orchestrator.

### `PipelineConfig` (envelope)

The config is an envelope (runner-agnostic fields) plus a discriminated
payload. Today only `pipeline_kind="pixel"` exists; a future vector
pipeline adds a sibling `vector` payload on the same envelope.

| Field           | Type                  | Default              | Description                                                                          |
| --------------- | --------------------- | -------------------- | ------------------------------------------------------------------------------------ |
| `pipeline_kind` | `Literal["pixel"]`    | `"pixel"`            | Discriminator selecting the payload shape                                            |
| `ee_expression` | `str`                 | *required*           | Serialized EE computation. Opaque — DatensEE never interprets it. Must be valid JSON |
| `gee_project`   | `str`                 | *required*           | GCP project with the EE API enabled (used in the HV API URL)                         |
| `runner`        | `RunnerConfig`        | local mode           | Runner selection                                                                     |
| `snapshot_time` | `int \| None`         | `None`               | Unix **microseconds** the expression's asset versions were pinned to (snapshot pinning). Diagnostic on the Java side — pinning is baked into `ee_expression` |
| `carryover_file`| `str \| None`         | `None`               | Staged by `datensee retry`: NDJSON of the previous round's no-progress records. The pipeline unions these lines into `_failures.json` alongside this run's fresh failures |
| `pixel`         | `PixelPayload`        | *required*           | Pixel payload: `{tile_grid, output}`                                                 |

Legacy flat input (`tile_grid` / `output` at the top level) is migrated
to the nested `pixel` payload by a `model_validator(mode="before")`, so
pre-discriminator callers and JSON files keep working.

Computed properties:

- `tile_count` — number of inline tiles (0 when externalized to a file)
- `raw_output_bytes` — exact uncompressed output size, computed from `tile_count × tile_size² × bytes_per_pixel × band_count`
- `effective_output_tile_size_pixels` — output COG edge length, falling back to compute tile size
- `expected_output_tile_count` — number of output COGs (distinct `(out_row, out_col)` groups in two-tier mode)

Cross-field validation:

- `pipeline_kind="pixel"` requires the `pixel` payload
- `output.output_tile_size_pixels`, when set, must be a multiple of `tile_grid.tile_size_pixels` and ≥ it
- `ee_expression` must parse as JSON

### `TileGrid`

| Field              | Type                       | Default | Description                                                          |
| ------------------ | -------------------------- | ------- | -------------------------------------------------------------------- |
| `pixel_grid`       | `PixelGrid`                | *required* | Parent export grid (see below)                                    |
| `tile_size_pixels` | `int`                      | `512`   | Compute tile edge length in pixels (> 0)                             |
| `tiles`            | `list[TileCoordinate]`     | `None`  | Inline tile coordinates                                              |
| `tiles_file`       | `str`                      | `None`  | GCS URI or local path to an NDJSON file of tile coordinates          |

Exactly one of `tiles` or `tiles_file` must be set. Externalize when the
inline form would inflate the config beyond a few MB (tens of thousands
of tiles). Convenience properties: `crs` (→ `pixel_grid.crs_code`) and
`pixel_size` (→ `affine_transform.scale_x`).

### `PixelGrid`

The canonical export shape — mirrors EE's own `PixelGrid` type and
is sent verbatim to `computePixels`.

| Field              | Type              | Description                                        |
| ------------------ | ----------------- | -------------------------------------------------- |
| `crs_code`         | `str`             | EPSG code or proj string, e.g. `EPSG:4326`         |
| `affine_transform` | `AffineTransform` | `{scale_x, shear_x, translate_x, shear_y, scale_y, translate_y}`. `scale_y` is negative (NW-corner origin) |
| `dimensions`       | `GridDimensions`  | `{width, height}` in pixels                        |

### `TileCoordinate`

Tiles are **integer pixel rectangles** inside the parent `PixelGrid` —
float bboxes are derived from `transform × pixel_offsets` and never
persisted, so cross-export grid alignment is exact by construction.

| Field      | Type        | Default | Description                                                                                          |
| ---------- | ----------- | ------- | ---------------------------------------------------------------------------------------------------- |
| `col_px`   | `int`       | *required* | Local pixel column offset from the parent grid's origin (≥ 0)                                     |
| `row_px`   | `int`       | *required* | Local pixel row offset (≥ 0)                                                                      |
| `width_px` | `int`       | *required* | Tile width in pixels (> 0). Quadtree split children halve each axis                               |
| `height_px`| `int`       | *required* | Tile height in pixels (> 0)                                                                       |
| `row`      | `int`       | `0`     | Compute tile row in the export bbox. Pinned to the root compute tile under adaptive retries          |
| `col`      | `int`       | `0`     | Compute tile column                                                                                  |
| `out_row`  | `int`       | `0`     | Output tile row (two-tier tiling, `row_px // output_tile_size_pixels`). Equals `row` when two-tier tiling is disabled |
| `out_col`  | `int`       | `0`     | Output tile column                                                                                   |
| `lineage`  | `list[int]` | `[]`    | Quadtree path from the root compute tile to a sub-tile (each entry 0–3). Empty = root compute tile |

### `OutputConfig`

| Field                     | Type           | Default     | Description                                                                                                  |
| ------------------------- | -------------- | ----------- | ------------------------------------------------------------------------------------------------------------ |
| `output_path`             | `str`          | *required*  | GCS URI (`gs://…`) or local directory                                                                        |
| `band_count`              | `int`          | `1`         | Number of output bands (> 0)                                                                                 |
| `data_type`               | enum           | `float32`   | One of `float32`, `float64`, `int16`, `int32`, `uint8`, `uint16`                                             |
| `output_tile_size_pixels` | `int \| None`  | `None`      | two-tier tiling. Must be a positive multiple of `tile_grid.tile_size_pixels` and ≥ it                     |
| `merge_existing_output`   | `bool`         | `False`     | Set by `datensee retry`: the assembler decodes an already-existing output COG as the baseline canvas and overlays this run's tiles (including quadtree split children). Fresh exports leave this off so stale files are replaced, never blended into |
| `nodata`                  | `float`        | unset       | Written as the `GDAL_NODATA` tag on every output COG. EE returns masked pixels as 0 with no mask channel — `unmask(sentinel)` the expression and declare the sentinel here |
| `compression`             | enum           | `deflate`   | `deflate` or `none` — the only COG knob (no overviews, no predictor; block size is always the compute tile size) |

### `RunnerConfig`

| Field      | Type                       | Default  | Description                                          |
| ---------- | -------------------------- | -------- | ---------------------------------------------------- |
| `mode`     | `Literal["dataflow", "local"]` | `local`  | Runner selection                                |
| `dataflow` | `DataflowRunnerConfig \| None` | `None`   | Required when `mode == "dataflow"`              |

### `DataflowRunnerConfig`

| Field                   | Type                  | Default          | Description                                                                                |
| ----------------------- | --------------------- | ---------------- | ------------------------------------------------------------------------------------------ |
| `project`               | `str`                 | *required*       | GCP project for the Dataflow job                                                           |
| `region`                | `str`                 | *required*       | Dataflow region                                                                            |
| `temp_location`         | `str`                 | *required*       | GCS URI for Dataflow temp files                                                            |
| `staging_location`      | `str`                 | *required*       | GCS URI for Dataflow staging files                                                         |
| `machine_type`          | `str`                 | `n2-standard-4`  | Worker machine type                                                                        |
| `num_workers`           | `int`                 | `4`              | Initial worker count. Dataflow's batch autoscaler is reactive, so booting non-trivially reaches steady state faster |
| `max_workers`           | `int`                 | `100`            | Autoscaling ceiling (> 0)                                                                  |
| `autoscaling_algorithm` | `Literal["THROUGHPUT_BASED", "NONE"]` | `THROUGHPUT_BASED` | Pinned so behavior doesn't drift with the Beam runner version              |
| `number_of_worker_harness_threads` | `int`      | `8`              | Per-worker fetcher concurrency — above vCPU count because tile fetches are I/O-bound       |
| `service_account_email` | `str \| None`         | `None`           | Worker service account override                                                            |
| `network`               | `str \| None`         | `None`           | VPC network                                                                                |
| `subnetwork`            | `str \| None`         | `None`           | VPC subnetwork                                                                             |
| `labels`                | `dict[str, str] \| None` | `None`        | Dataflow job labels, forwarded as `--labels=JSON`. Useful for filtering `jobs.list` later  |

## Costs and quotas

### Earth Engine

Non-commercial use of Earth Engine — including calls through the High
Volume API — stays free under existing terms. DatensEE doesn't change
how EE bills compute; it's a parallel client of the public HV
endpoint. Commercial users continue to bill EE under their existing
agreement.

### Dataflow + GCS

The new line item is **Dataflow**. Running an export at scale requires a
GCP project with billing enabled, and you'll pay standard rates for
workers, shuffle, and GCS storage. For non-commercial EE users this is
typically the only new cost; for commercial users it's another line on
the invoice they already get.

DatensEE prints a cost estimate before submitting any large job. For
small / dev iteration use `--runner local` — it skips the ~2 minute VM
warm-up and runs in-process.

### HV API quotas

The High Volume endpoint enforces per-project request budgets. A
realistic continental-scale export will saturate the default quickly.
Quota uplifts go through the same channel they always have — see the
[Earth Engine usage and quota docs](https://developers.google.com/earth-engine/guides/usage)
for the current process.

DatensEE has **no client-side QPS limiter** — deliberately. EE's quota
system is the rate-shaping signal: workers respond to 429s with
exponential backoff. Throughput is controlled by worker parallelism
(`num_workers` / `max_workers` × `number_of_worker_harness_threads`),
so cap the worker count to run conservatively on default quotas and
raise it after a quota review.

## Troubleshooting

### "Expression file is not valid JSON"

`ee_expression` must be the output of `ee.serializer.encode(image)`
serialized to a JSON string — not a Python `ee.Image` object, not the
output of `image.serialize()` on very old earthengine-api clients
(legacy serialization). Modern clients serialize in cloud-API form by
default, and the Python API accepts a live `ee.Image` directly — no
manual serialization at all.

### "Region GeoJSON type is 'FeatureCollection'"

Pass a single Polygon or MultiPolygon, or a Feature wrapping one.
Extract one feature first if your source is a FeatureCollection.

### "Dataflow mode requires a GCS output path"

`--output` (or the `output=` arg) must be a `gs://…` URI when running on
Dataflow. Local directories only work with `--runner local`.

### "--temp-location is required for Dataflow mode"

Dataflow needs a writable GCS prefix for temp + staging files. DatensEE
derives the staging location from `temp_location` by appending `/staging`.

### "The first IFD does not immediately follow the TIFF header"

This is Earth Engine rejecting an output as a non-COG. DatensEE writes
its own COG layout (first IFD at offset 8) so this should not happen
with current builds. If you see it, your JAR is likely stale —
`datensee jar download` to refresh.

### Tiles come back masked / "Corrupted tile"

LZW round-tripping has had bugs across reader/writer combinations, which
is why DatensEE writes `deflate` (or `none`) only — `compress` values
other than those two are rejected at config validation. If a very old
config file sets `lzw`, change it to `deflate`.

### Quota errors / 429s from the HV API

Occasional 429s are normal and handled by exponential backoff inside the
workers. Sustained 429 storms mean the project's HV quota is saturated:
lower `max_workers` (or `number_of_worker_harness_threads`) to shrink
concurrent in-flight requests, and request a quota uplift before scaling
back up.

### "Dataflow job submitted under wrong project"

When DatensEE is driven service-side from a webapp acting on behalf of
a user, the Dataflow `createJob` call has to attribute its quota and
API-enablement checks to the *user's* project, not the webapp's
service account. Pass the user's `Credentials` to `export(...,
credentials=user_creds)`. Setting ADC alone is not enough — see the
[service-side auth note](#service-side-auth-credentials).

### Partial failures

A few failed tiles in a 10 000-tile job is normal. The pipeline writes a
structured failures journal (`{output}/_failures.json`, NDJSON) covering
both fetch failures and write-stage failures (transcode/assembly/upload,
message prefixed `write-stage:`). Run `datensee retry --output <output>`
to re-fetch just the failed tiles — EE complexity failures are split
into quadtree children, and in two-tier mode the re-fetched tiles are merged
into the existing output COGs. See
[retry-with-journal.md](retry-with-journal.md).

### Masked pixels come back as 0

EE's `computePixels` GeoTIFF has no mask channel: masked pixels are
returned as literal 0, indistinguishable from a real zero (an NDVI of
0 is data!). If your expression can produce zeros or has masked areas,
`unmask(sentinel)` it with a sentinel outside the data range and pass
`--nodata SENTINEL` — every output COG then carries a `GDAL_NODATA`
tag and QGIS/rasterio/GDAL treat the sentinel as transparent:

```python
image = my_image.unmask(-9999)
```
```
datensee export expr.json region.geojson ... --nodata=-9999
```

Blocks that failed and were zero-filled by the assembler are recorded
in `_failures.json`; recover them with `datensee retry --until-done`.

### Large COGs feel slow in desktop GIS

DatensEE writes COGs without overview pyramids (EE's `loadGeoTIFF`
doesn't need them, and every extra IFD is wasted bytes for
machine-to-machine pipelines). For interactive use in QGIS/ArcGIS at
low zoom, add overviews as a post-step:

```
gdaladdo -r average tile_r0000_c0000.tif 2 4 8 16 32
```

or batch: `for f in out/*.tif; do gdaladdo -r average "$f" 2 4 8 16 32; done`.
