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
| `notebook`     | `matplotlib`          | Notebook display helpers (tile maps, tile previews)       |
| `validation`   | `rasterio`            | `datensee validate` post-export integrity checks          |
| `all`          | both of the above     | Convenience                                               |

Example: `pip install 'datensee[notebook]'`.

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
Use `--output-tile-size` (M6 two-tier tiling) to control how many
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
[`status`](#datensee-status), [`validate`](#datensee-validate),
[`jar`](#datensee-jar).

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
| `--output-tile-size`          | int    | unset          | M6 two-tier tiling — output COG edge in pixels, must be a positive multiple of `--tile-size`. Unset = one COG per compute tile |
| `--runner`                    | str    | `local`        | `local` (in-process Beam) or `dataflow`                                              |
| `--region-gcp`                | str    | `us-central1`  | Dataflow region                                                                      |
| `--temp-location`             | str    | unset          | GCS URI for Dataflow temp/staging files. Required when `--runner dataflow`           |
| `--jar`                       | path   | auto-detected  | Path to the pipeline JAR                                                             |
| `--max-qps`                   | int    | `100`          | Max EE HV API queries per second across all workers                                  |
| `--dry-run`                   | flag   | off            | Print the pipeline command without executing                                         |
| `--yes`, `-y`                 | flag   | off            | Skip the confirmation prompt for jobs > 10 000 tiles                                 |
| `--validate / --no-validate`  | flag   | `--no-validate`| Run zero-cost output checks after the pipeline completes (local mode only)          |

Note: the CLI default for `--runner` is `local`; the Python API default
is `dataflow`. The CLI bias toward local matches a "try it, then scale
it" workflow; the API bias toward Dataflow matches programmatic /
service-side use.

### `datensee status`

Poll a Dataflow job to a terminal state.

| Argument / Flag      | Type | Default       | Description                |
| -------------------- | ---- | ------------- | -------------------------- |
| `JOB_ID` (positional)| str  | *required*    | Dataflow job ID            |
| `--project`          | str  | *required*    | GCP project ID             |
| `--region-gcp`       | str  | `us-central1` | Dataflow region            |

Exits 0 on `JOB_STATE_DONE`, non-zero otherwise.

### `datensee validate`

Run the post-export check suite (E01–E10) against an output directory or
GCS prefix. Requires the `validation` extra (`pip install 'datensee[validation]'`).

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

### `datensee.export(...)`

Validates inputs, tiles the region, builds the pipeline config, and
submits the job. For Dataflow mode, returns immediately after submission
with a `job_id`. For local mode, blocks until the pipeline completes.

| Param               | Type                                | Default       | Description                                                                                                                                    |
| ------------------- | ----------------------------------- | ------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| `ee_expression`     | `str`                               | *required*    | Serialized EE computation — JSON string from `ee.serializer.encode()`                                                                          |
| `region`            | `dict`                              | *required*    | GeoJSON Polygon or MultiPolygon geometry dict (WGS84). A Feature wrapping one is also accepted                                                |
| `project`           | `str`                               | *required*    | GCP project with the EE API enabled                                                                                                            |
| `output`            | `str`                               | *required*    | GCS URI (`gs://…`) for Dataflow, or a local directory for `runner="local"`                                                                     |
| `scale`             | `float`                             | `30.0`        | Pixel size in meters                                                                                                                           |
| `crs`               | `str`                               | `"EPSG:4326"` | Target CRS — EPSG code or proj string                                                                                                          |
| `tile_size`         | `int`                               | `512`         | Compute tile edge in pixels (one HV API request per compute tile)                                                                              |
| `output_tile_size`  | `int \| None`                       | `None`        | M6 two-tier tiling. When set, must be a positive multiple of `tile_size`. `None` = one COG per compute tile                                    |
| `runner`            | `Literal["local", "dataflow"]`      | `"dataflow"`  | Runner mode                                                                                                                                    |
| `region_gcp`        | `str`                               | `"us-central1"` | Dataflow region                                                                                                                              |
| `temp_location`     | `str \| None`                       | `None`        | GCS URI for Dataflow temp/staging files. Required when `runner="dataflow"`                                                                     |
| `max_qps`           | `int`                               | `100`         | Max EE HV API queries per second across all workers                                                                                            |
| `labels`            | `dict[str, str] \| None`            | `None`        | Dataflow job labels, forwarded as `--labels=JSON`. Useful for filtering `jobs.list` queries downstream. Only applied in `dataflow` mode        |
| `jar`               | `Path \| str \| None`               | `None`        | Path to the pipeline JAR. Auto-detected when `None`                                                                                            |
| `dry_run`           | `bool`                              | `False`       | Validate and build the config but don't submit                                                                                                 |
| `progress_callback` | `Callable[[int, int], None] \| None`| `None`        | Local-mode progress callback `(completed, total)`. Ignored for Dataflow                                                                        |
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
| `region`    | `dict`  | *required*    | GeoJSON Polygon or MultiPolygon geometry dict (WGS84)        |
| `scale`     | `float` | `30.0`        | Pixel size in meters                                         |
| `crs`       | `str`   | `"EPSG:4326"` | Target CRS                                                   |
| `tile_size` | `int`   | `512`         | Tile edge size in pixels                                     |

Returns: `TileGrid` (see [Pipeline config](#pipeline-config)).

### `datensee.poll(...)`

Poll a Dataflow job until it reaches a terminal state.

| Param           | Type                                | Default         | Description                                                                                  |
| --------------- | ----------------------------------- | --------------- | -------------------------------------------------------------------------------------------- |
| `job_id`        | `str`                               | *required*      | Dataflow job ID                                                                              |
| `project`       | `str`                               | *required*      | GCP project ID                                                                               |
| `region`        | `str`                               | `"us-central1"` | Dataflow region                                                                              |
| `callback`      | `Callable[[JobInfo], None] \| None` | `None`          | Per-tick callback. When `None`, uses the Rich Live display (CLI mode)                        |
| `poll_interval` | `int`                               | `15`            | Seconds between polls                                                                        |

Returns: final `JobState`.

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

### `PipelineConfig` (top-level)

| Field           | Type                  | Default              | Description                                                                          |
| --------------- | --------------------- | -------------------- | ------------------------------------------------------------------------------------ |
| `ee_expression` | `str`                 | *required*           | Serialized EE computation. Opaque — DatensEE never interprets it. Must be valid JSON |
| `gee_project`   | `str`                 | *required*           | GCP project with the EE API enabled (used in the HV API URL)                         |
| `tile_grid`     | `TileGrid`            | *required*           | Tile decomposition                                                                   |
| `output`        | `OutputConfig`        | *required*           | Destination and format                                                               |
| `runner`        | `RunnerConfig`        | local mode           | Runner selection                                                                     |
| `rate_limit`    | `RateLimitConfig`     | `max_qps=100`        | EE HV API rate limiting                                                              |

Computed properties:

- `tile_count` — number of inline tiles (0 when externalized to a file)
- `raw_output_bytes` — exact uncompressed output size, computed from `tile_count × tile_size² × bytes_per_pixel × band_count`
- `effective_output_tile_size_pixels` — output COG edge length, falling back to compute tile size

Cross-field validation:

- `output.output_tile_size_pixels`, when set, must be a multiple of `tile_grid.tile_size_pixels` and ≥ it
- `ee_expression` must parse as JSON

### `TileGrid`

| Field              | Type                       | Default | Description                                                          |
| ------------------ | -------------------------- | ------- | -------------------------------------------------------------------- |
| `crs`              | `str`                      | *required* | Target CRS                                                        |
| `scale_meters`     | `float`                    | *required* | Pixel size in meters (> 0)                                        |
| `tile_size_pixels` | `int`                      | `512`   | Compute tile edge length in pixels (> 0)                             |
| `tiles`            | `list[TileCoordinate]`     | `None`  | Inline tile coordinates                                              |
| `tiles_file`       | `str`                      | `None`  | GCS URI or local path to an NDJSON file of tile coordinates          |

Exactly one of `tiles` or `tiles_file` must be set. Externalize when the
inline form would inflate the config beyond a few MB (tens of thousands
of tiles).

### `TileCoordinate`

| Field    | Type        | Default | Description                                                                                          |
| -------- | ----------- | ------- | ---------------------------------------------------------------------------------------------------- |
| `x_min`  | `float`     | *required* | Tile bounding box, target CRS                                                                     |
| `y_min`  | `float`     | *required* |                                                                                                   |
| `x_max`  | `float`     | *required* |                                                                                                   |
| `y_max`  | `float`     | *required* |                                                                                                   |
| `row`    | `int`       | *required* | Compute tile row in the export bbox. Pinned to the root compute tile under adaptive retries        |
| `col`    | `int`       | *required* | Compute tile column                                                                                |
| `out_row`| `int`       | `0`     | Output tile row (M6 two-tier tiling). Equals `row` when two-tier tiling is disabled                  |
| `out_col`| `int`       | `0`     | Output tile column                                                                                   |
| `lineage`| `list[int]` | `[]`    | Quadtree path from the root compute tile to a sub-tile (each entry 0–3). Empty = root compute tile |

### `OutputConfig`

| Field                     | Type           | Default     | Description                                                                                                  |
| ------------------------- | -------------- | ----------- | ------------------------------------------------------------------------------------------------------------ |
| `output_path`             | `str`          | *required*  | GCS URI (`gs://…`) or local directory                                                                        |
| `band_count`              | `int`          | `1`         | Number of output bands (> 0)                                                                                 |
| `data_type`               | enum           | `float32`   | One of `float32`, `float64`, `int16`, `int32`, `uint8`, `uint16`                                             |
| `output_tile_size_pixels` | `int \| None`  | `None`      | M6 two-tier tiling. Must be a positive multiple of `tile_grid.tile_size_pixels` and ≥ it                     |
| `cog`                     | `CogParameters`| defaults    | COG layout / compression                                                                                     |

### `CogParameters`

| Field             | Type        | Default                | Description                                                       |
| ----------------- | ----------- | ---------------------- | ----------------------------------------------------------------- |
| `overview_levels` | `list[int]` | `[2, 4, 8, 16, 32]`    | Decimation factors for COG overviews                              |
| `blocksize`       | `int`       | `512`                  | Internal tile size (> 0)                                          |
| `compress`        | enum        | `lzw`                  | `lzw`, `deflate`, `zstd`, or `none`. Pipeline default is `deflate` (see [handoff.md](handoff.md))           |
| `predictor`       | enum        | `2`                    | `1` (none), `2` (horizontal int), `3` (floating-point)            |

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
| `max_workers`           | `int`                 | `100`            | Autoscaling ceiling (> 0)                                                                  |
| `service_account_email` | `str \| None`         | `None`           | Worker service account override                                                            |
| `network`               | `str \| None`         | `None`           | VPC network                                                                                |
| `subnetwork`            | `str \| None`         | `None`           | VPC subnetwork                                                                             |
| `labels`                | `dict[str, str] \| None` | `None`        | Dataflow job labels, forwarded as `--labels=JSON`. Useful for filtering `jobs.list` later  |

### `RateLimitConfig`

| Field     | Type  | Default | Description                                                  |
| --------- | ----- | ------- | ------------------------------------------------------------ |
| `max_qps` | `int` | `100`   | Maximum EE HV API queries per second across all workers (>0) |

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

`max_qps` is the global QPS ceiling DatensEE shares across all workers.
Set it conservatively at first (well under your project's quota), run a
small job, then ratchet up. Per-tile retries are classified separately
and don't double-count against this limit.

## Troubleshooting

### "Expression file is not valid JSON"

`ee_expression` must be the output of `ee.serializer.encode(image)`
serialized to a JSON string — not a Python `ee.Image` object, not the
output of `image.serialize()` (which is a different format).

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

LZW round-tripping has had bugs across reader/writer combinations.
DatensEE defaults to `deflate` for that reason. If you've manually set
`compress=lzw`, switch to `deflate`.

### Quota errors / 429s from the HV API

Lower `--max-qps` (or `max_qps=` in Python). The default of 100 is fine
for most projects on default quota; halve it if you hit ratelimit errors
in the worker logs. Then request a quota uplift via the EE usage docs
linked above before raising it back up.

### "Dataflow job submitted under wrong project"

When DatensEE is driven service-side from a webapp acting on behalf of
a user, the Dataflow `createJob` call has to attribute its quota and
API-enablement checks to the *user's* project, not the webapp's
service account. Pass the user's `Credentials` to `export(...,
credentials=user_creds)`. Setting ADC alone is not enough — see the
[service-side auth note](#service-side-auth-credentials).

### Partial failures

A few failed tiles in a 10 000-tile job is normal. The pipeline writes a
manifest of failed tiles next to the output; re-run with that manifest
as input rather than re-running the whole job. (Targeted re-run
ergonomics are still being polished — see [retry-with-journal.md](retry-with-journal.md).)
