# Case Study: 21,316-Tile Distributed Export

*Date: 2026-09-02. Workload benchmark of the Dataflow execution path on a 21,316-tile extraction.*

## Workload Configuration

The benchmark exported SRTM 30 m elevation data (`USGS/SRTMGL1_003`, int16)
across a 20°×20° bounding box over West and Central Africa (10°W–10°E, 5°S–15°N)
in EPSG:4326:

```bash
datensee export srtm.json region.geojson \
  --project datensee-testing \
  --output gs://datensee-testing-central/e2e/scale-20260902T124354Z \
  --runner dataflow --region-gcp us-east1 \
  --machine-type e2-standard-4 --num-workers 2 --max-workers 16 --yes
```

The region decomposed to **21,316 compute tiles** (a 146×146 grid of 512×512
pixel tiles). Because this exceeded the 5,000-tile inline limit, the tile
coordinate list was externalized to `{output}/_tiles.ndjson` (2.5 MB).

## Execution Metrics

| Metric | Measurement |
|---|---|
| Submission to worker execution | 1 min 40 s (launcher) + 1 min 28 s (worker provisioning) |
| Total wall-clock time | **10 min 14 s** |
| Tiles fetched, transcoded, written | **21,316 / 21,316** (zero failures in `_failures.json`) |
| Steady-state throughput | ~47 tiles/s across 2 workers (16 total fetch threads) |
| Output volume | 21,316 COGs, **1.59 GB** total (~75 KB/tile, Deflate compression) |

The Dataflow autoscaler remained at the initial 2 workers throughout execution
because throughput processed the input queue faster than the scale-up threshold
required for an I/O-bound SRTM query.

## Resource Costs

| Resource | Consumption | Cost |
|---|---|---|
| Earth Engine compute | 5,344 EECU-s (1.48 EECU-h, ~0.25 EECU-s/tile) | $0 (non-commercial); ~$0.59 commercial ($0.40/EECU-h) |
| Dataflow vCPU | 8,118 vCPU-s (2.25 vCPU-h) | $0.13 |
| Dataflow memory | 9.0 GB-h | $0.03 |
| Dataflow persistent disk / shuffle | 14 GB-h / 6.5 MB | < $0.01 |
| Inter-region network egress (us-east1 to us-central1) | 1.6 GB | ~$0.02 |
| Cloud Storage storage | 1.59 GB | $0.03/month |
| **Total direct execution cost** | | **~$0.18** |

*(Execution ran in `us-east1` due to zone capacity constraints in `us-central1`.
Co-locating workers with the destination bucket eliminates inter-region egress
costs.)*

## Key Observations

- **Compute profile:** Pre-computed assets such as SRTM average ~0.25
  EECU-seconds per tile. For multi-temporal composites and reducers, EECU
  consumption forms the dominant cost factor rather than Dataflow worker compute.
- **Throughput stability without client rate limiting:** 16 concurrent High
  Volume API fetch threads sustained ~47 tiles/s with zero dropped requests.
  Per-worker exponential backoff on HTTP 429 responses kept requests within
  project quota boundaries.
- **Scaling projections:** A full continental US (CONUS) SRTM export (~500,000
  tiles at 30 m) projects to ~35 EECU-hours, ~$4 in Dataflow compute, and ~37 GB
  of GeoTIFF storage.

## Validation Findings

Running `datensee validate` against the 21,316 exported rasters identified two
configuration improvements:

1. **Explicit data type declarations:** The initial run defaulted configuration
   metadata to `float32`, whereas the source dataset is `int16`. While all pixel
   values and affine parameters were correct, `--band-count` and `--data-type`
   flags were added to the CLI and metadata sidecar to allow precise schema
   declarations.
2. **Sparse tile size handling:** 10,481 tiles covering ocean areas in the Gulf
   of Guinea compressed to ~935 bytes under Deflate compression. An arbitrary
   1 KB minimum size threshold previously flagged these as truncated files; the
   check was updated so that `rasterio` decodes the file directly whenever
   available.

Following these adjustments, structural integrity checks passed across all
21,316 files (`integrity PASS, 21,316/21,316`).

Job ID `2026-09-02_05_43_59-6816905880710455580`.
