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

## Collaborator quick start (private beta)

The repo is private, so running the demo notebook requires a GitHub token and a GCP project. Three steps:

**1. Create a GitHub token**

Go to [github.com/settings/tokens](https://github.com/settings/tokens) → *Generate new token (classic)* → tick **`repo`** → set an expiry → copy it.

**2. Add it as a Colab Secret**

Open the notebook in Colab → click the key icon (🔑) in the left sidebar → *Add new secret* → name it **`GITHUB_TOKEN`**, paste the token, enable notebook access.

**3. Set your GCP project and run**

In the notebook's setup cell, change `PROJECT` and `GCS_BUCKET` to your own GCP project and bucket, then run all cells.

Your GCP project needs:
- [Earth Engine API](https://console.cloud.google.com/apis/library/earthengine.googleapis.com) enabled
- A GCS bucket to write output to
- The account you auth with in Colab registered for [non-commercial EE use](https://earthengine.google.com/noncommercial) (or a commercial license)

[![Open in Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/michaelfdewitt/datensee/blob/master/notebooks/datensee_demo_gcs.ipynb)

---

## Why

Earth Engine is great at computing things, but the built-in `Export.image.*` functions were designed in an era that predates the Cambrian explosion of easy and affordable cloud processing tools.

DatensEE doesn't recompile or interpret your computation — EE does that.
We just call the [High Volume API](https://developers.google.com/earth-engine/reference/rest/v1/projects.image/computePixels)
thousands of times in parallel, with proper tiling, retries, and rate limiting,
and stitch the results together.

**EE is the computation engine. DatensEE is the parallelism engine.**

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
4. **Output** lands as Cloud Optimized GeoTIFF(s) on GCS, or as local GeoTIFFs
   with a VRT mosaic for the direct runner

## Quick start

### Prerequisites

- Python 3.12+, [uv](https://docs.astral.sh/uv/)
- Java 25+, Gradle 9+
- `gcloud auth application-default login` (ADC configured)
- A GCP project with the [Earth Engine API](https://console.cloud.google.com/apis/library/earthengine.googleapis.com) enabled

### Install

```bash
pip install datensee
```

Or from source:

```bash
cd cli && uv sync
```

### Pipeline JAR

The Java pipeline JAR is required to run exports. Install it with:

```bash
# Download a prebuilt JAR from GitHub Releases
datensee jar download

# Or build from source (requires Java 25+ and Gradle)
datensee jar build
```

The CLI searches for the JAR in this order:
1. `--jar <path>` flag (explicit)
2. `DATENSEE_JAR` environment variable
3. `~/.datensee/jars/datensee-pipeline.jar` (from `datensee jar download`)
4. Development repo path (`pipelines/build/libs/datensee-pipeline.jar`)

### Demo: Landsat 9 NDVI over SF Bay Area

```bash
datensee demo --project my-gcp-project --output ./ndvi-output
```

This fetches a small Landsat 9 NDVI composite (~4 tiles at 30m) using the
local direct runner. Output is individual GeoTIFFs plus a `mosaic.vrt`.

Convert to a single Cloud Optimized GeoTIFF:

```bash
gdal_translate -of COG -co COMPRESS=LZW ndvi-output/mosaic.vrt ndvi.tif
```

### Full export

```bash
# Serialize your EE expression
python -c "
import ee, json
ee.Initialize()
image = (ee.ImageCollection('LANDSAT/LC09/C02/T1_L2')
         .filterDate('2023-06-01', '2023-09-01')
         .median()
         .normalizedDifference(['SR_B5', 'SR_B4']))
with open('expr.json', 'w') as f:
    json.dump(ee.serializer.encode(image, for_cloud_api=True), f)
"

# Export at 10m in UTM, multi-band
datensee export expr.json region.geojson \
  --project my-gcp-project \
  --output gs://my-bucket/exports/ndvi \
  --scale 10 \
  --crs EPSG:32610 \
  --runner dataflow \
  --temp-location gs://my-bucket/tmp
```

## Notebook / Colab

DatensEE works natively in Jupyter notebooks and Google Colab — no shell commands needed.

```python
import datensee
from datensee import notebook

notebook.ensure_auth()  # triggers Colab OAuth flow, exports ADC for Java

grid = datensee.tile(region, scale=30.0)
notebook.display_tile_grid(grid, region)       # matplotlib preview

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
- **Edge clipping** — the EE expression is automatically wrapped in
  `Image.clip(region)` so edge tiles return nodata outside the boundary
  instead of wasting EECUs on out-of-bounds pixels. The tile *grid* stays
  full-size for alignment; only the *computation* is clipped.
- **Adjacent tile contiguity** — `tile[i].x_max == tile[i+1].x_min` exactly,
  with no floating-point gaps or overlaps

This is tested via integration tests that shift a region by N pixels, fetch
tiles from both grids, and assert pixel-by-pixel equality in the overlap.

## Configuration

The pipeline config is a JSON contract between the Python CLI and the Java pipeline:

```jsonc
{
  "ee_expression": "{ ... }",       // opaque — EE evaluates this, we don't touch it
  "gee_project": "my-project",
  "tile_grid": {
    "crs": "EPSG:32610",
    "scale_meters": 10.0,
    "tile_size_pixels": 512,
    "tiles": [ ... ]
  },
  "output": {
    "output_path": "gs://bucket/prefix",
    "band_count": 3,
    "data_type": "uint8",            // float32, float64, int16, int32, uint8, uint16
    "cog": { "compress": "lzw", "blocksize": 512 }
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
│   │   ├── api.py       Public Python API (export, demo, poll, tile)
│   │   ├── notebook.py  Colab/Jupyter detection, auth, HTML displays
│   │   ├── main.py      CLI entrypoint (thin wrapper around api.py)
│   │   ├── config.py    Pipeline config models
│   │   ├── tiling.py    Region → globally-aligned tile grid
│   │   ├── assemble.py  VRT mosaic assembly (multi-band, any data type)
│   │   ├── submit.py    Dataflow job submission
│   │   └── eval/        Output validation: 10 evals (structural, spatial, pixel-level)
│   └── tests/           Unit + EE HV API integration + eval tests
├── pipelines/           Java Beam pipeline (Gradle)
│   └── src/main/java/com/datensee/
│       ├── DatensEEPipeline.java
│       ├── fetch/       HV API client, retry, rate limiting
│       └── io/          COG writer
├── notebooks/           Colab/Jupyter examples
├── contract/            JSON schema + examples
└── CLAUDE.md            Development guide
```

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
