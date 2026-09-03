<h1 align="center">DatensEE</h1>

<p align="center">
  <strong>Massively parallel Earth Engine exports via Cloud Dataflow</strong>
</p>

<p align="center">
  <code>pip install datensee</code>
</p>

---

> **Demo software, not an official Google product.** DatensEE is a proof of
> concept built to demonstrate a pattern. It is unsupported, may break as Earth
> Engine or Dataflow evolve, and may be removed without notice. Learn from it and
> build on it; don't use it as a production tool.

DatensEE parallelizes Google Earth Engine image exports across Cloud Dataflow
workers using Earth Engine's [High Volume
API](https://developers.google.com/earth-engine/reference/rest/v1/projects.image/computePixels).
Given a serialized EE expression, a target region, and a scale or exact grid
specification, it decomposes the region into aligned tiles, fetches them
concurrently, and writes Cloud Optimized GeoTIFFs (COGs) to Google Cloud
Storage or local disk.

```bash
datensee export expression.json region.geojson \
  --project my-gcp-project --output gs://my-bucket/ndvi-conus \
  --scale 10 --crs EPSG:32610
```

- **Drop-in replacement for batch export:** Accepts the same computation
  graph from Python, the CLI, or the Code Editor. Earth Engine evaluates the
  expression per tile; DatensEE handles distribution and raster assembly.
- **Local or Dataflow execution:** Direct runner for small regions and
  testing; Dataflow Flex Templates for multi-worker cloud runs using the same
  configuration.
- **Self-describing output:** Writes standard COGs readable directly by GIS
  tools without auxiliary manifests or GDAL worker dependencies.
- **Partial failure recovery:** Employs exponential backoff on HTTP 429
  responses, records failed tiles in a structured journal (`_failures.json`),
  and supports iterative recovery via `datensee retry --until-done`.
- **Deterministic geometry:** Snaps tiles to an integer grid anchored at
  the CRS origin. Snapshot pinning ensures all workers evaluate against a
  consistent view of mutable Earth Engine assets.

### When to use DatensEE

DatensEE is intended for exports that exceed the limits, timeouts, or
throughput of `Export.image.toCloudStorage`. For smaller single-machine tasks,
consider [xee](https://github.com/google/xee) or standard batch exports. For
architectural details and trade-offs, see [docs/architecture.md](docs/architecture.md).

> **Note on costs:** Dataflow bills for worker VMs, storage, and egress even
> when the Earth Engine project is noncommercial. Noncommercial status waives
> EE compute charges (EECUs), but Google Cloud infrastructure charges still
> apply. See the [scale run case study](docs/case-study-scale-run.md) for an
> itemized billing breakdown. Also: the High Volume API caches less than Earth
> Engine's batch export path, so the same computation can consume significantly
> more EECU-time (and, on a commercial project, cost more) than
> `Export.image.toCloudStorage`.

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
enabled, and authenticated application default credentials (`gcloud auth application-default login`).

```bash
pip install datensee
```

`datensee` contains pure-Python dependencies only. It does not require `earthengine-api` or a local Java toolchain; the pipeline JAR downloads automatically on first run via `datensee jar download`. (To construct expressions in Python, install `earthengine-api` separately.)

```bash
datensee demo --project my-gcp-project --output ./ndvi-output
```

The demo fetches a small Landsat 9 NDVI composite (9 tiles at 30 m) using the local
runner and writes standard COGs (`tile_r0000_c0000.tif`, ...). To produce fewer,
larger files, pass `--output-tile-size` (a multiple of `--tile-size` that covers
the region) to assemble multi-block COGs per output tile.

For a full walkthrough (project setup, first export, debugging, recipes), see the
[user guide](docs/guide/README.md).

## Usage

### Python API

Pass a live `ee.Image` and region directly to `datensee.export()`. The client
serializes the object using your local Earth Engine installation without
importing `ee` inside DatensEE:

```python
import ee, datensee
ee.Initialize()

image = (ee.ImageCollection('LANDSAT/LC09/C02/T1_L2')
         .filterDate('2023-06-01', '2023-09-01')
         .median()
         .normalizedDifference(['SR_B5', 'SR_B4']))
region = ee.Geometry.Rectangle([-122.6, 37.2, -121.8, 38.0])

result = datensee.export(
    image, region,
    project="my-gcp-project", output="gs://my-bucket/exports/ndvi",
    scale=10, crs="EPSG:32610", temp_location="gs://my-bucket/tmp",
)
```

`region` accepts an `ee.Geometry`, GeoJSON dictionary, or Shapely geometry.
It can be omitted if the image has a defined footprint.

### CLI

```bash
datensee export expr.json region.geojson \
  --project my-gcp-project --output gs://my-bucket/exports/ndvi \
  --scale 10 --crs EPSG:32610 \
  --runner dataflow --temp-location gs://my-bucket/tmp
```

### Earth Engine Code Editor (JavaScript)

When using the JavaScript Code Editor, the snippet in
[`docs/code-editor-snippet.js`](docs/code-editor-snippet.js) outputs a single
JSON bundle containing both the expression and region:

```bash
datensee export bundle.json --project my-gcp-project \
  --output gs://my-bucket/exports/ndvi --scale 10 --crs EPSG:32610
```

### Exact Grid Alignment

By default, `--scale` derives an origin-snapped grid. To align output
pixel-for-pixel with an existing asset or reference export, specify
`--crs-transform` and `--dimensions` (mirroring Earth Engine's `crsTransform`
and `dimensions`):

```bash
datensee export expr.json region.geojson \
  --project my-gcp-project --output gs://my-bucket/exports/ndvi \
  --crs EPSG:32610 --crs-transform "10,0,512340,0,-10,4183400" \
  --dimensions 10240x10240
```

`--scale` and `--crs-transform`/`--dimensions` are mutually exclusive.
Dimensions must be a multiple of `--tile-size`. In Python:
`pixel_grid=datensee.PixelGrid(...)`.

### Notebook / Colab

DatensEE runs in Jupyter and Colab environments:

```python
import datensee
from datensee import notebook

notebook.ensure_auth()  # Colab OAuth, exports ADC for the JVM
result = datensee.export(expression, region, project="...", output="gs://...",
                         runner="dataflow", temp_location="gs://...")
notebook.display_job_progress(result.job_id, project="...")  # HTML polling
```

Walkthrough:
[`notebooks/datensee_quickstart.ipynb`](notebooks/datensee_quickstart.ipynb).

## Local vs Dataflow

|  | Local (`runner="local"`) | Dataflow (`runner="dataflow"`) |
|---|---|---|
| Good for | Small regions, testing | Large regions, production |
| Tile count | Up to ~100 | Thousands to millions |
| Startup | Seconds (JVM) | ~2 min (VM + container) |
| Parallelism | Single-threaded | Auto-scales to hundreds of workers |
| Cost | Free (local machine) | Dataflow vCPU/GB-hours |

Dataflow incurs approximately two minutes of VM and worker container
provisioning overhead. For workloads under ~100 tiles, use the local runner.
For benchmarks and itemized costs from a 21,316-tile export, see
[docs/case-study-scale-run.md](docs/case-study-scale-run.md).

## Tiling and Pixel Alignment

Tile grids snap to a global origin at `(0, 0)` in the target CRS:

- **Cross-export alignment:** Exports sharing the same scale and CRS produce
  identical pixel grids across overlapping areas.
- **Region intersection:** Tiles outside the region polygon are dropped
  during decomposition. DatensEE does not automatically wrap expressions in
  `Image.clip()`; call `.clip()` directly if server-side masking is desired.
- **Adjacency:** Tiles are defined as integer pixel coordinates within the
  parent `PixelGrid`, avoiding floating-point seam artifacts.

## Configuration

The pipeline configuration is a JSON contract between the Python CLI and the
Java pipeline. The envelope contains `pipeline_kind`, the opaque `ee_expression`,
`gee_project`, `snapshot_time` (Unix µs), a `pixel` payload (the canonical
`pixel_grid` sent to `computePixels`, tile coordinates, and output settings),
and a `runner` block. Full schema:
[`contract/pipeline-config.schema.json`](contract/pipeline-config.schema.json);
worked example: [`contract/examples/`](contract/examples/).

## Repository layout

- `cli/`: Python CLI and library (Typer + Pydantic): tiling, submission,
  polling, output validation. `pixel/` holds raster-specific components.
- `pipelines/`: Java Beam pipeline (Gradle): tile fetching, pure-Java COG
  transcoding, and GCS output.
- `contract/`: Python↔Java JSON configuration schema and examples.
- `notebooks/`: Colab quickstart.
- `docs/`: the [user guide](docs/guide/README.md), [architecture](docs/architecture.md),
  [releasing](docs/releasing.md), and the [scale case study](docs/case-study-scale-run.md).

## Development

To work on DatensEE directly:

```bash
cd cli && uv sync                    # Python
cd pipelines && ./gradlew shadowJar  # Java
```

Tests:

```bash
cd cli && uv run pytest -v                          # Unit tests (no network)
cd cli && uv run pytest tests/test_integration_ee.py \
  --integration --gee-project=YOUR_PROJECT -v       # Integration tests against EE HV API
cd pipelines && ./gradlew test                      # Java tests
```

The CLI locates the pipeline JAR from `--jar`, `DATENSEE_JAR`,
`~/.datensee/jars/`, or local build outputs. Manual management commands
include `datensee jar download` and `datensee jar build`.

## License

Apache License 2.0, see [LICENSE](LICENSE).
