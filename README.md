<h1 align="center">DatensEE</h1>

<p align="center">
  <strong>Massively parallel Earth Engine exports via Cloud Dataflow</strong>
</p>

<p align="center">
  <code>pip install datensee</code>
</p>

---

DatensEE takes the computation you already wrote in Earth Engine and runs it at
continental scale. You bring the expression, the region, and the scale — DatensEE
handles tiling, parallel fetching across thousands of Dataflow workers, and
assembly into Cloud Optimized GeoTIFFs on GCS.

```
datensee export expression.json region.geojson \
  --project my-gcp-project \
  --output gs://my-bucket/ndvi-conus \
  --scale 10 \
  --crs EPSG:32610
```

## Why

Earth Engine is great at computing things, but the built-in `Export.image.*` functions were designed in an era that predates the Cambrian explosion of easy and affordable cloud processing tools.

DatensEE doesn't recompile or interpret your computation — EE does that.
We just call the [High Volume API](https://developers.google.com/earth-engine/reference/rest/v1/projects.image/computePixels)
thousands of times in parallel, with proper tiling, retries, and backoff,
and stitch the results together.

**EE is the computation engine. DatensEE is the parallelism engine.**

### Why isn't this fifty lines?

A fair question — "call `computePixels` in parallel and stitch the results" sounds
like a `ThreadPoolExecutor` and rasterio. That version exists, and it's the right
tool up to roughly a few GB of output on one machine. DatensEE is what's left after
committing to the regime where it falls over: terabyte-scale exports, thousands of
concurrent workers, and hours-long jobs where partial failure is the normal case,
not the exceptional one. Nearly all of the code is the consequence of three
commitments:

1. **No native dependencies on workers.** Dataflow workers don't ship GDAL, and we
   didn't want custom containers or native-library versioning. So COG output is a
   pure-Java TIFF transcoder (`CogTranscoder`), and stitching thousands of tiles
   into multi-block COGs — including merging retried tiles into *existing* COGs —
   is a streaming assembler (`AssembledCogWriter`), not an in-memory mosaic.

2. **Partial failure is the steady state.** When 50 of 10,000 fetches fail,
   abort-and-rerun is not an answer. That buys: 429-driven exponential backoff,
   classification of EE's error signatures (transient vs. too-complex vs.
   terminal), a structured failures journal, quadtree splitting of tiles EE
   rejects as too expensive, and a `retry --until-done` loop that converges
   instead of retrying into the same wall forever. The naive version's answer to
   failure is "run it again" — at this scale that's hours and real money.

3. **Precision guarantees that are invisible until violated.** Every tile is an
   integer pixel rectangle in one canonical grid, so a retried sub-tile lands
   pixel-exact in an existing COG by arithmetic, not float tolerance. And every
   worker sees the same snapshot of every EE asset (snapshot pinning) — without
   it, a collection that mutates mid-export gives tile A and tile B different
   worlds, and the output is silently wrong. Both bugs exist in the naive version;
   nobody notices until they diff outputs.

The rest is deliberate product surface — job submission and polling, cost
estimation before you spend money, Colab auth, progress display — because the API
is the product and EE users aren't infra engineers. The tests outnumber the source;
that's the price of the word "pixel-exact" above.

If your exports fit comfortably on one machine, use
[xee](https://github.com/google/xee) or `ee.batch.Export` and be happy. DatensEE
exists for when they stop fitting.

## How it works

```
                          ┌──────────────────────────────┐
                          │     Earth Engine Backend      │
                          │  (evaluates your computation  │
                          │   per-tile via HV endpoint)   │
                          └──────────▲───────────────────┘
                                     │ High Volume API
                                     │ (thousands of concurrent tile fetches)
                                     │
┌──────────────┐          ┌──────────┴────────────────────┐          ┌────────────┐
│  DatensEE    │─────────▶│     Cloud Dataflow            │─────────▶│    GCS     │
│  CLI         │ submits  │                               │ writes   │   (COG)    │
│              │ job      │  Tile coords → ParDo: fetch   │ output   │            │
│  • Validate  │          │  → Assemble raster            │          │            │
│  • Tile      │          │  → Write COG                  │          │            │
│  • Submit    │          │                               │          │            │
└──────────────┘          └───────────────────────────────┘          └────────────┘
```

1. **You provide** a serialized EE expression + region GeoJSON + scale + CRS
2. **Python CLI** validates inputs, decomposes the region into a globally-aligned
   tile grid, and submits a Dataflow job (or runs locally for small regions)
3. **Java Beam pipeline** fans out across workers — each fetches tiles via the
   HV API with exponential backoff and retries
4. **Output** lands as a directory of self-describing Cloud Optimized GeoTIFFs —
   on GCS for Dataflow, or a local directory for the direct runner. No manifest
   files; any modern GIS tool opens them directly

## Quick start

### Prerequisites

- Python 3.12+
- A GCP project with the [Earth Engine API](https://console.cloud.google.com/apis/library/earthengine.googleapis.com) enabled
- `gcloud auth application-default login` (only needed for the local CLI; Colab handles auth automatically)

### Install

```bash
pip install datensee
```

That's it. No `earthengine-api` dependency, no Java toolchain, no Gradle. The package ships with a small set of pure-Python deps (Typer, Pydantic, httpx, Rich, pyproj, shapely, google-auth, google-cloud-storage). The Java pipeline JAR is fetched on first use by `datensee jar download` (auto-invoked on first `export`), so users never see Gradle.

### Where it lives

DatensEE is published to PyPI as `datensee` and lives in the `earthengine` repo under `tools/datensee/`. It is **deliberately separate** from the `earthengine` package itself — installing `datensee` does **not** pull in `earthengine-api` or any other heavy GIS toolchain. Users who want to author EE expressions can `pip install earthengine-api` independently.

### Demo: Landsat 9 NDVI over SF Bay Area

```bash
datensee demo --project my-gcp-project --output ./ndvi-output
```

This fetches a small Landsat 9 NDVI composite (~4 tiles at 30m) using the
local direct runner. Output is a directory of Cloud Optimized GeoTIFFs
(`tile_r0000_c0000.tif`, …) — self-describing files that QGIS, rasterio, or
any modern GIS opens directly, no manifest needed.

Want a single big COG instead of many small ones? Use two-tier tiling:
pass `--output-tile-size` (a multiple of `--tile-size`, large enough to
cover the region) and the pipeline assembles compute tiles into one
multi-block COG per output tile.

### Full export — swap the `Export` call

If you already have an `ee.Image` in Python, DatensEE is a drop-in
replacement for `Export.image.toCloudStorage` — hand it the live object
and your region; no serializer incantations:

```python
import ee, datensee
ee.Initialize()

image = (ee.ImageCollection('LANDSAT/LC09/C02/T1_L2')
         .filterDate('2023-06-01', '2023-09-01')
         .median()
         .normalizedDifference(['SR_B5', 'SR_B4']))
region = ee.Geometry.Rectangle([-122.6, 37.2, -121.8, 38.0])

# before: Export.image.toCloudStorage(image, region=region, scale=10, ...)
result = datensee.export(
    image, region,
    project="my-gcp-project",
    output="gs://my-bucket/exports/ndvi",
    scale=10, crs="EPSG:32610",
    temp_location="gs://my-bucket/tmp",
)
```

`region` also accepts a GeoJSON dict or a shapely geometry, and
`datensee` never imports `ee` itself — your installed client does the
serialization. From the shell, the same export takes a serialized
expression file + GeoJSON region:

```bash
datensee export expr.json region.geojson \
  --project my-gcp-project \
  --output gs://my-bucket/exports/ndvi \
  --scale 10 --crs EPSG:32610 \
  --runner dataflow --temp-location gs://my-bucket/tmp
```

Coming from the Code Editor (JavaScript)? See
[`docs/task-import.md`](docs/task-import.md) for the planned
copy-the-job-description path.

## Notebook / Colab

DatensEE works natively in Jupyter notebooks and Google Colab — no shell commands needed.

```python
import datensee
from datensee import notebook

notebook.ensure_auth()  # triggers Colab OAuth flow, exports ADC for Java

grid = datensee.tile(region, scale=30.0)

result = datensee.export(expression, region, project="...", output="gs://...", runner="dataflow", temp_location="gs://...")
notebook.display_job_progress(result.job_id, project="...")  # HTML status polling
```

See [`notebooks/datensee_quickstart.ipynb`](notebooks/datensee_quickstart.ipynb) for a full walkthrough.

### Local vs Dataflow: when to use which

| | Local (`runner="local"`) | Dataflow (`runner="dataflow"`) |
|---|---|---|
| **Good for** | Small regions, testing, debugging | Large regions, production exports |
| **Tile count** | Up to ~100 tiles | Thousands to millions |
| **Startup time** | Seconds (JVM only) | ~2 min (VM provisioning + container boot) |
| **Parallelism** | Single-threaded | Auto-scales to hundreds of workers |
| **Cost** | Free (your machine) | Dataflow vCPU/GB-hours |

Dataflow has ~2 minutes of fixed overhead for VM provisioning, container startup, and shuffle infrastructure — regardless of workload size. For 9 tiles this dominates the wall time; for 10,000 tiles it's negligible. **Use local mode for anything under ~100 tiles.**

## Tiling & pixel alignment

DatensEE snaps tile grids to a global origin at `(0, 0)` in the target CRS.
This guarantees:

- **Pixel-perfect alignment** — two independent exports at the same scale and
  CRS produce identical pixel grids in any overlapping area
- **Region-aware tiling, no clipping** — tiles that don't intersect the
  region are dropped at decompose time, so out-of-region areas never cost
  EECUs. The EE expression itself is *not* modified — DatensEE deliberately
  does not wrap it in `Image.clip()`. (Call `.clip()` on your image
  yourself if you want EE-side masking explicitly.)
- **Adjacent tile contiguity** — tiles are integer pixel rectangles
  (`col_px`, `row_px`, `width_px`, `height_px`) inside one parent
  `PixelGrid`, so adjacency is exact by construction — no floating-point
  gaps or overlaps

This is tested via integration tests that shift a region by N pixels, fetch
tiles from both grids, and assert pixel-by-pixel equality in the overlap.

## Configuration

The pipeline config is a JSON contract between the Python CLI and the Java pipeline:

```jsonc
{
  "pipeline_kind": "pixel",          // discriminator; future: "vector"
  "ee_expression": "{ ... }",        // opaque — EE evaluates this, we don't touch it
  "gee_project": "my-project",
  "snapshot_time": 1715000000000000, // Unix µs — asset versions pinned to this moment
  "pixel": {
    "tile_grid": {
      "pixel_grid": {                // canonical export grid, sent verbatim to computePixels
        "crs_code": "EPSG:32610",
        "affine_transform": { "scale_x": 10.0, "translate_x": 500000.0,
                              "scale_y": -10.0, "translate_y": 4200000.0,
                              "shear_x": 0, "shear_y": 0 },
        "dimensions": { "width": 4096, "height": 4096 }
      },
      "tile_size_pixels": 512,
      "tiles": [                     // integer pixel rects inside the parent grid
        { "col_px": 0, "row_px": 0, "width_px": 512, "height_px": 512, "row": 0, "col": 0 }
      ]
    },
    "output": {
      "output_path": "gs://bucket/prefix",
      "band_count": 3,
      "data_type": "uint8",          // float32, float64, int16, int32, uint8, uint16
      "output_tile_size_pixels": 2048, // optional two-tier tiling
      "cog": { "compress": "deflate" } // or "none"
    }
  },
  "runner": {
    "mode": "dataflow",              // or "local"
    "dataflow": { "project": "...", "region": "us-central1", ... }
  }
}
```

Full schema: [`contract/pipeline-config.schema.json`](contract/pipeline-config.schema.json)

## Project structure

```
datensee/
├── cli/                 Python CLI + library (Typer + Pydantic)
│   ├── src/datensee/
│   │   ├── api.py       Public Python API (export, demo, poll, tile, retry)
│   │   ├── notebook.py  Colab/Jupyter detection, auth, HTML displays
│   │   ├── main.py      CLI entrypoint (thin wrapper around api.py)
│   │   ├── config.py    PipelineConfig envelope (kind discriminator, runner, …)
│   │   ├── submit.py    Dataflow job submission
│   │   ├── status.py    Job polling + watchdog cost controls
│   │   ├── meta.py      _export_meta.json sidecar (used by retry)
│   │   ├── pinning.py   EE snapshot-time pinning
│   │   └── pixel/       Pixel-pipeline subpackage
│   │       ├── config.py     PixelGrid, TileGrid, OutputConfig
│   │       ├── tiling.py     Region → globally-aligned tile grid
│   │       ├── retry.py      Quadtree splitter + failures-journal I/O
│   │       └── validation/   Output validation (structural, spatial, pixel-level)
│   └── tests/           Unit + EE HV API integration + output-validation tests
├── pipelines/           Java Beam pipeline (Gradle)
│   └── src/main/java/com/datensee/
│       ├── DatensEEPipeline.java   Kind-agnostic pipeline shell
│       ├── fetch/       Shared EE HV primitives (auth, error classification)
│       └── pixel/       Pixel pipeline: fetch DoFns + COG writers (io/)
├── notebooks/           Colab/Jupyter examples
├── contract/            JSON schema + examples
└── CLAUDE.md            Development guide
```

## Development setup

Most users should `pip install datensee` and stop reading. This section is for contributors working on DatensEE itself.

```bash
# Python side
cd cli && uv sync

# Java side (only needed if you're modifying the pipeline)
cd pipelines && ./gradlew shadowJar
```

The CLI looks for the pipeline JAR in this order:

1. `--jar <path>` flag
2. `DATENSEE_JAR` environment variable
3. `~/.datensee/jars/datensee-pipeline.jar` (downloaded by `datensee jar download`)
4. Repo development path (`pipelines/build/libs/datensee-pipeline.jar`)

End users never need step 2–4: `datensee` auto-downloads the prebuilt JAR on first use. `datensee jar download` and `datensee jar build` are exposed as escape hatches.

## Testing

```bash
# Unit tests (no network, fast)
cd cli && uv run pytest -v

# Integration tests — hits the real EE HV API
# Fetches ~4000 SRTM tiles across EPSG:4326, EPSG:32610, EPSG:3857
cd cli && uv run pytest tests/test_integration_ee.py \
  --integration --gee-project=YOUR_PROJECT -v

# Java tests
cd pipelines && ./gradlew test
```

The integration suite includes:
- **Single-tile fetches** across 3 CRS variants and 4 expression types (elevation, slope, rescaled, multi-band)
- **Tiling + fetch** — decompose a region, fetch every tile, validate GeoTIFF responses
- **High-volume batch** — 1000–2000 concurrent tile fetches with failure rate assertions
- **Pixel alignment** — shift region by N pixels, fetch overlapping tiles, assert `array_equal`
- **Config roundtrip** — serialize to JSON, deserialize, fetch from restored config

## License

Apache License 2.0 — see [LICENSE](LICENSE).
