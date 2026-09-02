<h1 align="center">DatensEE</h1>

<p align="center">
  <strong>Massively parallel Earth Engine exports via Cloud Dataflow</strong>
</p>

<p align="center">
  <code>pip install datensee</code>
</p>

---

DatensEE runs the Earth Engine computation you already have at continental
scale. Bring the expression, a region, and a scale; it tiles the region, fetches
every tile in parallel through the [High Volume
API](https://developers.google.com/earth-engine/reference/rest/v1/projects.image/computePixels),
and assembles Cloud Optimized GeoTIFFs on GCS.

```bash
datensee export expression.json region.geojson \
  --project my-gcp-project --output gs://my-bucket/ndvi-conus \
  --scale 10 --crs EPSG:32610
```

- **Drop-in for `Export.image.toCloudStorage`.** Same expression graph, from
  Python, the CLI, or Colab. DatensEE never interprets your computation; EE
  still evaluates every pixel.
- **Local or Dataflow.** Direct runner for small jobs, Dataflow Flex Template
  for thousands of workers, same config.
- **Self-describing output.** A directory of COGs any GIS tool opens. No VRT, no
  manifest, no GDAL on the workers.
- **Built for partial failure.** 429 backoff, a failures journal, and
  `retry --until-done` that converges. A stalled 5% of tiles never means
  rerunning the whole job.
- **Pixel-exact.** Tiles are integer rectangles in one canonical grid; snapshot
  pinning gives every worker one consistent view of mutable collections.

**EE is the computation engine. DatensEE is the parallelism engine.** Everything
upstream of pixels (your algorithm, assets, masking) is untouched, so there is
nothing to re-validate. Everything downstream (tiling, fan-out, failure
recovery, assembly) is handled here instead of rebuilt by every team that hits
the same wall.

**When to use it.** The day a task outgrows `Export.image.toCloudStorage`:
regions too large, resolutions too fine, jobs that queue for hours or fail
outright. If it still fits `Export.image` or one machine, use
[xee](https://github.com/google/xee) or `ee.batch.Export`. Curious why this is
not fifty lines of `ThreadPoolExecutor`? See [docs/design.md](docs/design.md).

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
│  CLI         │ submits  │  Tile coords → fetch → assemble│ writes   │   (COG)    │
│              │ job      │  → write COG                   │ output   │            │
└──────────────┘          └───────────────────────────────┘          └────────────┘
```

1. **You provide** a serialized EE expression, a region, and scale + CRS (or an
   exact grid).
2. **The Python CLI** validates inputs, decomposes the region into a
   globally-aligned tile grid, and submits a Dataflow job (or runs locally).
3. **The Java Beam pipeline** fans out across workers, each fetching tiles via
   the HV API with backoff and retries, and writes one COG per output tile.

## Quick start

**Prerequisites:** Python 3.12+, a GCP project with the [Earth Engine
API](https://console.cloud.google.com/apis/library/earthengine.googleapis.com)
enabled, and `gcloud auth application-default login` (local CLI only; Colab
handles auth).

```bash
pip install datensee
```

Pure-Python deps only (Typer, Pydantic, httpx, Rich, pyproj, shapely,
google-auth, google-cloud-storage). No `earthengine-api`, no Java toolchain, no
Gradle. The pipeline JAR is fetched on first `export` (`datensee jar download`),
so you never see Gradle. Authoring EE expressions is a separate
`pip install earthengine-api`.

```bash
datensee demo --project my-gcp-project --output ./ndvi-output
```

The demo fetches a small Landsat 9 NDVI composite (9 tiles at 30 m) on the local
runner and writes COGs (`tile_r0000_c0000.tif`, ...) that QGIS or rasterio open
directly. For one big COG instead of many small ones, pass `--output-tile-size`
(a multiple of `--tile-size`, large enough to cover the region) and the pipeline
assembles a single multi-block COG per output tile.

## Usage

### Python: drop in for `Export.image`

Hand `export` a live `ee.Image` and a region. No serializer incantations;
`datensee` never imports `ee`, so your own client does the serialization.

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
    project="my-gcp-project", output="gs://my-bucket/exports/ndvi",
    scale=10, crs="EPSG:32610", temp_location="gs://my-bucket/tmp",
)
```

`region` also accepts a GeoJSON dict or a shapely geometry, and is optional if
the image carries a footprint (DatensEE reads `image.geometry()`).

### CLI

```bash
datensee export expr.json region.geojson \
  --project my-gcp-project --output gs://my-bucket/exports/ndvi \
  --scale 10 --crs EPSG:32610 \
  --runner dataflow --temp-location gs://my-bucket/tmp
```

### From the Code Editor: one file, not two

Prototyping in the EE Code Editor (JavaScript)? The snippet in
[`docs/code-editor-snippet.js`](docs/code-editor-snippet.js) prints a single
*bundle* with both the serialized computation and the region. Paste it into one
file and run, with no separate `expr.json` and `region.geojson`:

```bash
datensee export bundle.json --project my-gcp-project \
  --output gs://my-bucket/exports/ndvi --scale 10 --crs EPSG:32610
```

### Exact grid: pixel-for-pixel comparison

`--scale` derives a grid snapped to a global origin (for geographic CRSs the
pixel size uses the equator constant). To line output up *exactly* with another
export or an existing asset, so a per-pixel diff is meaningful, pin the grid
verbatim, mirroring EE's own `crsTransform` + `dimensions`:

```bash
datensee export expr.json region.geojson \
  --project my-gcp-project --output gs://my-bucket/exports/ndvi \
  --crs EPSG:32610 --crs-transform "10,0,512340,0,-10,4183400" \
  --dimensions 10240x10240
```

`--scale` and `--crs-transform`/`--dimensions` are mutually exclusive.
Dimensions must be a whole multiple of `--tile-size` (partial edge tiles are not
supported yet). In Python: `pixel_grid=datensee.PixelGrid(...)`.

### Notebook / Colab

DatensEE runs natively in Jupyter and Colab, no shell needed.

```python
import datensee
from datensee import notebook

notebook.ensure_auth()  # Colab OAuth, exports ADC for the JVM
result = datensee.export(expression, region, project="...", output="gs://...",
                         runner="dataflow", temp_location="gs://...")
notebook.display_job_progress(result.job_id, project="...")  # HTML polling
```

Full walkthrough:
[`notebooks/datensee_quickstart.ipynb`](notebooks/datensee_quickstart.ipynb).

## Local vs Dataflow

|  | Local (`runner="local"`) | Dataflow (`runner="dataflow"`) |
|---|---|---|
| Good for | Small regions, testing | Large regions, production |
| Tile count | Up to ~100 | Thousands to millions |
| Startup | Seconds (JVM) | ~2 min (VM + container) |
| Parallelism | Single-threaded | Auto-scales to hundreds of workers |
| Cost | Free (your machine) | Dataflow vCPU/GB-hours |

Dataflow carries ~2 min of fixed provisioning overhead regardless of size; for 9
tiles it dominates, for 10,000 it is negligible. **Use local under ~100 tiles.**
A real 21,316-tile SRTM run (wall time, EECU-seconds, the itemized ~$0.18 bill)
is in [docs/case-study-scale-run.md](docs/case-study-scale-run.md).

## Tiling and pixel alignment

Tile grids snap to a global origin at `(0, 0)` in the target CRS, which
guarantees:

- **Cross-export alignment.** Two exports at the same scale and CRS produce
  identical pixel grids in any overlap.
- **No wasted EECUs.** Tiles that miss the region are dropped at decompose time.
  The expression is never wrapped in `Image.clip()` (call `.clip()` yourself for
  EE-side masking).
- **Exact adjacency.** Tiles are integer pixel rectangles inside one parent
  `PixelGrid`, so there are no floating-point gaps or overlaps.

Pinned by integration tests that shift a region by N pixels, fetch both grids,
and assert pixel equality in the overlap.

## Configuration

The pipeline config is a JSON contract between the Python CLI and the Java
pipeline. The envelope carries `pipeline_kind`, the opaque `ee_expression`,
`gee_project`, `snapshot_time` (Unix µs; asset versions pin here), a `pixel`
payload (the canonical `pixel_grid` sent verbatim to `computePixels`, plus tiles
and output settings), and a `runner` block. Full schema:
[`contract/pipeline-config.schema.json`](contract/pipeline-config.schema.json);
worked example: [`contract/examples/`](contract/examples/).

## Repository layout

- `cli/`: Python CLI and library (Typer + Pydantic): tiling, submission,
  polling, output validation. `pixel/` holds the raster-specific pieces.
- `pipelines/`: Java Beam pipeline (Gradle): per-tile fetch, pure-Java COG
  transcoding, GCS write.
- `contract/`: the Python↔Java JSON config schema and examples.
- `notebooks/`: Colab quickstart.
- `docs/`: [architecture](docs/architecture.md), [design
  rationale](docs/design.md), [releasing](docs/releasing.md), and the [scale
  case study](docs/case-study-scale-run.md).

## Development

Most users `pip install datensee` and stop here. To work on DatensEE itself:

```bash
cd cli && uv sync                    # Python
cd pipelines && ./gradlew shadowJar  # Java (only when changing the pipeline)
```

Tests:

```bash
cd cli && uv run pytest -v                          # unit (no network)
cd cli && uv run pytest tests/test_integration_ee.py \
  --integration --gee-project=YOUR_PROJECT -v       # real EE HV API
cd pipelines && ./gradlew test                      # Java
```

The CLI resolves the pipeline JAR from `--jar`, then `DATENSEE_JAR`, then
`~/.datensee/jars/`, then a repo build. End users never touch this; the JAR
auto-downloads on first use, with `datensee jar download` / `jar build` as
escape hatches.

## License

Apache License 2.0, see [LICENSE](LICENSE).
