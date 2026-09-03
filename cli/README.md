# DatensEE

**Massively parallel Earth Engine exports via Cloud Dataflow.**

> **Demo software, not an official Google product.** DatensEE is a proof of
> concept built to demonstrate a pattern. It is unsupported, may break as Earth
> Engine or Dataflow evolve, and may be removed without notice. Learn from it and
> build on it; don't use it as a production tool.

> **Cost caveat:** the High Volume API caches less than Earth Engine's batch
> export path, so the same computation can consume significantly more EECU-time
> (and, on a commercial project, cost more) than `Export.image.toCloudStorage`.

DatensEE takes the computation you already wrote in Earth Engine and runs it at
continental scale. You bring the expression, the region, and the scale: DatensEE
handles tiling, parallel fetching across thousands of Dataflow workers, and
assembly into Cloud Optimized GeoTIFFs on GCS.

```bash
pip install datensee
```

No `earthengine-api` dependency, no Java toolchain. Cloud exports launch a
prebuilt Dataflow Flex Template; the local runner (small regions, debugging)
fetches a prebuilt pipeline JAR on first use.

## Drop-in replacement for `Export.image.toCloudStorage`

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
    project="my-gcp-project",
    output="gs://my-bucket/exports/ndvi",
    scale=10, crs="EPSG:32610",
    runner="dataflow", temp_location="gs://my-bucket/tmp",
)
```

`region` also accepts a GeoJSON dict or a shapely geometry. From the shell:

```bash
datensee export expr.json region.geojson \
  --project my-gcp-project \
  --output gs://my-bucket/exports/ndvi \
  --scale 10 --crs EPSG:32610 \
  --runner dataflow --temp-location gs://my-bucket/tmp
```

Try it end to end with the built-in demo (local runner, ~4 tiles):

```bash
datensee demo --project my-gcp-project --output ./ndvi-output
```

## How it works

EE is the computation engine; DatensEE is the parallelism engine. It never
interprets or recompiles your expression; it decomposes the region into a
globally aligned tile grid, calls the
[High Volume API](https://developers.google.com/earth-engine/reference/rest/v1/projects.image/computePixels)
once per tile from a Dataflow pipeline (429-driven backoff, retries, a
structured failures journal with `datensee retry --until-done`), and
transcodes each result into self-describing COGs. Every tile is an integer
pixel rectangle in one canonical grid, and every worker sees the same
snapshot of every EE asset, so outputs are pixel-exact and reproducible.

| | Local (`runner="local"`) | Dataflow (`runner="dataflow"`) |
|---|---|---|
| **Good for** | Small regions, testing | Large regions, production |
| **Tile count** | Up to ~100 | Thousands to millions |
| **Startup** | Seconds (JVM) | ~2 min (VM + container boot) |
| **Cost** | Free (your machine) | Dataflow vCPU/GB-hours |

## Prerequisites

- Python 3.12+
- A GCP project with the Earth Engine API enabled
- `gcloud auth application-default login` (Colab handles auth automatically via
  `datensee.notebook.ensure_auth()`)

## Links

- Source, issues, and full documentation:
  <https://github.com/michaelfdewitt/datensee>
- Colab quickstart:
  <https://github.com/michaelfdewitt/datensee/blob/master/notebooks/datensee_quickstart.ipynb>

Apache License 2.0.
