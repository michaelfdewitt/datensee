# Your first export

> Draft. The skeleton and commands are real; prose will be expanded.

Two runs: the local demo (fast, free, no cloud), then the same job on Dataflow.
Do the local one first. It confirms auth and Earth Engine access without spending
anything, so if it fails you know the problem is not Dataflow.

## Local demo

```bash
datensee demo --project YOUR_PROJECT --output ./first-export
```

DatensEE prints an export summary (tile count, pixel size, estimated cost), then
fetches a small Landsat 9 NDVI composite on the local runner. A healthy run
finishes in seconds and writes to `./first-export`:

```
./first-export/
  tile_r0000_c0000.tif   ... one COG per tile (9 of them)
  _export_meta.json      the export's shape, used by `retry` and `validate`
  _failures.json         dead-letter journal (empty on a clean run)
  _pipeline-config.json  the config that produced this output
```

Open the tiles in QGIS (drag the folder in) or with rasterio:

```python
import rasterio
with rasterio.open("first-export/tile_r0000_c0000.tif") as ds:
    print(ds.profile)   # crs, transform, dtype, dimensions
```

> **Reading the summary.** _(expand: walk through each line of the export
> summary panel, including the cost estimate.)_

## The same job on Dataflow

Point the output and temp location at your bucket and switch the runner:

```bash
datensee demo --project YOUR_PROJECT \
  --output gs://YOUR_BUCKET/first-export \
  --runner dataflow --temp-location gs://YOUR_BUCKET/tmp \
  --region-gcp us-central1
```

DatensEE submits the job and prints its ID plus a `datensee status ...` command
to watch it. Expect about two minutes of apparent inactivity at the start: that
is Dataflow provisioning VMs and pulling the container, not a hang. Follow it
with `datensee status`; see [Debugging](03-debugging.md) if it stalls.

> **What is different from local.** _(expand: the launcher, worker startup, why
> Dataflow is slower for tiny jobs, when the crossover happens.)_

## Reading the result

The Dataflow output has the same shape as the local one, under your GCS prefix.
Confirm it is complete:

```bash
datensee validate gs://YOUR_BUCKET/first-export
```

_(expand: opening GCS COGs directly in QGIS/rasterio via `/vsigs/`, and what
`_export_meta.json` records.)_

## Next

- More exports (composites, large SRTM, exact grids): [Cookbook](04-cookbook.md).
- When something breaks: [Debugging](03-debugging.md).
