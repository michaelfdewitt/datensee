# Case study: a 21,316-tile export

*2026-09-02 — the first at-scale run of the Dataflow path. One command, ~10 minutes, ~$0.20.*

## The job

SRTM elevation (`USGS/SRTMGL1_003`, int16, spatially varying) over a 20°×20° window of
West/Central Africa (10°W–10°E, 5°S–15°N) at 30 m nominal scale in EPSG:4326:

```bash
datensee export srtm.json region.geojson \
  --project datensee-testing \
  --output gs://datensee-testing-central/e2e/scale-20260902T124354Z \
  --runner dataflow --region-gcp us-east1 \
  --machine-type e2-standard-4 --num-workers 2 --max-workers 16 --yes
```

That decomposes to **21,316 compute tiles** (146×146 grid of 512×512 px), well past the
5,000-tile threshold, so the tile list was externalized to `{output}/_tiles.ndjson`
(2.5 MB) rather than inlined in the pipeline config — this run was that path's first
outing on Dataflow.

## What happened

| | |
|---|---|
| Submitted → workers receiving work | 1 min 40 s (launcher) + 1 min 28 s (worker startup) |
| Wall clock, submit → `JOB_STATE_DONE` | **10 min 14 s** |
| Tiles fetched, transcoded, written | **21,316 / 21,316** — `_failures.json` empty |
| Steady-state throughput | ~47 tiles/s on 2 workers (16 fetch threads total) |
| Output | 21,316 COGs, **1.59 GB** total (~75 KB/tile, deflate) |

The autoscaler never scaled beyond the initial 2 workers: throughput cleared the backlog
faster than `THROUGHPUT_BASED` cared to react. Good for the wallet; it does mean
scaling-to-16 remains unexercised (an honest gap — a heavier per-tile computation would
force it, this one was I/O-bound end to end).

## The bill

| Resource | Usage | Cost |
|---|---|---|
| Earth Engine compute | 5,344 EECU-s = **1.48 EECU-h** (~0.25 EECU-s/tile) | $0 (non-commercial); ≈ $0.59 at the commercial $0.40/EECU-h |
| Dataflow vCPU | 8,118 vCPU-s = 2.25 vCPU-h | $0.13 |
| Dataflow memory | 9.0 GB-h | $0.03 |
| Dataflow PD / shuffle | 14 GB-h / 6.5 MB | < $0.01 |
| Inter-region egress (us-east1 workers → us-central1 bucket) | 1.6 GB | ~$0.02 |
| GCS storage | 1.59 GB | $0.03/month |
| **Total, one-time** | | **≈ $0.18** |

(Run in `us-east1` because `us-central1` was stocked out for e2 capacity that afternoon —
`--region-gcp` is exactly the knob for that; co-locating bucket and region would shave
the egress line.)

## Reading the numbers

- **EE is the engine; we are the throttle.** 0.25 EECU-s per SRTM tile is the floor for a
  pre-cached dataset; a real composite pipeline will dominate the bill through EECUs, not
  Dataflow — which is why the pre-submit panel quotes EECU ranges first.
- **No client-side rate limiter, no problem:** ~16 concurrent HV fetches sustained
  ~47 tiles/s with zero 429-driven failures reaching the journal. EE's own quota shaping
  plus per-tile backoff absorbed everything.
- **The journal-is-the-failure-model design never triggered** — 21,316/21,316 on the
  first attempt. The retry path was exercised separately with a synthetic journal
  (see the e2e log).
- Extrapolating linearly (this workload scales embarrassingly): a **CONUS-scale
  ~500k-tile SRTM export** ≈ 35 EECU-h + ~$4 of Dataflow + ~37 GB of COGs — an afternoon
  job on default quotas.

## What validating 21,316 files taught us

Running `datensee validate` against this output (it mirrors the prefix locally —
1.59 GB, parallel downloads — and checks every file's dimensions, bands, dtype,
CRS, and origin) found two real bugs, neither of them in the pixels:

1. **The config declared the wrong dtype.** `export` had no `--band-count` /
   `--data-type` flags, so every config claimed the default `float32` — and SRTM
   is `int16`. Dimensions, CRS, and origin matched to the digit on all 21,316
   files; only the declaration was wrong. Both flags now exist, are persisted in
   the meta sidecar, and are inherited by retry rounds.
2. **10,481 tiles are ocean.** The region includes the Gulf of Guinea; SRTM void
   compresses an all-zero 512×512 int16 tile to ~935 bytes, and a
   "file too small to be real" heuristic (1 KB) flagged every one — while
   rasterio read them all clean. The heuristic now applies only when rasterio is
   unavailable to do the actual check.

After both fixes: **integrity PASS, 21,316/21,316.** A validator earning its
keep by catching declaration drift, and a scale run earning its keep by
catching the validator.

Raw evidence (job id `2026-09-02_05_43_59-6816905880710455580`, metrics queries, message
log) in [`remote-e2e-log-2026-09-01.md`](remote-e2e-log-2026-09-01.md), Phase 7.
