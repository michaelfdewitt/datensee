# Cookbook

> Draft. Each recipe below is a runnable skeleton; expected output and variations
> will be expanded. Replace `YOUR_PROJECT` and `YOUR_BUCKET` throughout, and for
> cloud runs add `--runner dataflow --temp-location gs://YOUR_BUCKET/tmp`.

## Landsat NDVI composite

A median NDVI over a season, handed to DatensEE as a live `ee.Image` from Python.

```python
import ee, datensee
ee.Initialize()

image = (ee.ImageCollection('LANDSAT/LC09/C02/T1_L2')
         .filterDate('2023-06-01', '2023-09-01')
         .median()
         .normalizedDifference(['SR_B5', 'SR_B4']))
region = ee.Geometry.Rectangle([-122.6, 37.2, -121.8, 38.0])

datensee.export(
    image, region,
    project="YOUR_PROJECT", output="gs://YOUR_BUCKET/ndvi",
    scale=10, crs="EPSG:32610",
    runner="dataflow", temp_location="gs://YOUR_BUCKET/tmp",
)
```

_(expand: masking clouds first, why NDVI is float32, band naming.)_

## Multi-band seasonal composite

_(expand: a 3-band composite, `--band-count 3 --data-type uint8`, band order.)_

```bash
datensee export composite.json region.geojson \
  --project YOUR_PROJECT --output gs://YOUR_BUCKET/composite \
  --scale 10 --crs EPSG:32610 --band-count 3 --data-type uint8
```

## Large SRTM export

Elevation over a country-sized region. This is the shape of the
[scale case study](../case-study-scale-run.md) (21,316 tiles, about $0.18).

```bash
datensee export srtm.json region.geojson \
  --project YOUR_PROJECT --output gs://YOUR_BUCKET/srtm \
  --scale 30 --crs EPSG:4326 --data-type int16 \
  --runner dataflow --temp-location gs://YOUR_BUCKET/tmp \
  --max-workers 16
```

_(expand: choosing `--max-workers`, watching autoscale, the tiles-file path.)_

## Exact-grid comparison

Pin the output grid so it lines up pixel-for-pixel with another export or an
existing asset. `--scale` and `--crs-transform`/`--dimensions` are mutually
exclusive.

```bash
datensee export expr.json region.geojson \
  --project YOUR_PROJECT --output gs://YOUR_BUCKET/aligned \
  --crs EPSG:32610 \
  --crs-transform "10,0,512340,0,-10,4183400" \
  --dimensions 10240x10240
```

_(expand: reading an asset's native grid with `image.projection().getInfo()`,
diffing two outputs.)_

## One big COG instead of many (two-tier)

Group compute tiles into large output COGs with `--output-tile-size` (a multiple
of `--tile-size`, large enough to cover the region).

```bash
datensee export expr.json region.geojson \
  --project YOUR_PROJECT --output gs://YOUR_BUCKET/single \
  --scale 10 --crs EPSG:32610 \
  --tile-size 512 --output-tile-size 4096 \
  --runner dataflow --temp-location gs://YOUR_BUCKET/tmp
```

_(expand: file-count vs file-size tradeoff, internal block size.)_

## Recover a partial failure

Some tiles failed but the job finished. The failures are in
`gs://YOUR_BUCKET/OUTPUT/_failures.json`. Fill the gaps in place:

```bash
datensee retry --output gs://YOUR_BUCKET/OUTPUT \
  --runner dataflow --region-gcp us-central1 \
  --temp-location gs://YOUR_BUCKET/tmp --until-done
```

_(expand: what the retry decision does per error kind, split vs re-fetch.)_ See
[Debugging](03-debugging.md#partial-failure-the-journal-and-retry).
