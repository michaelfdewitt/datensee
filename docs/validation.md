# DatensEE Validation Reference

Two checks against exported output. For the rationale and architecture, see [`VALIDATION.md`](../VALIDATION.md).

## Usage

```bash
# Zero-cost integrity check
datensee validate ./output --config config.json

# Also compare pixels against fresh EE fetches (costs EECUs)
datensee validate ./output --config config.json --pixels --gee-project my-project

# Auto-run integrity after a local export
datensee export expr.json region.json -p my-project -o ./output --validate
```

```python
from datensee.pixel.validation import validate_output
from datensee.config import PipelineConfig

config = PipelineConfig.read_json("config.json")
report = validate_output("./output", config, pixels=True, sample=20)
assert report.all_passed
for r in report.results:
    print(r.check_id, r.status, r.message)
```

`validate_output(output_path, config, *, pixels=False, sample=20, gee_project=None)` returns a `ValidationReport` with `.all_passed`, `.results` (each a `CheckResult` with `.check_id`, `.status`, `.message`, `.details`), `.to_dict()`, and a Rich-renderable `.render()`.

## Output units

Both checks operate on **output units** — the files the pipeline actually writes (`datensee.pixel.validation.units`):

- **One-COG-per-compute-tile mode** (default): one unit per compute tile, named `tile_r{row:04d}_c{col:04d}.tif`, expected size = the tile's own `width_px × height_px` (quadtree split children are smaller and need not be square).
- **two-tier mode** (`output_tile_size_pixels` set): one unit per distinct `(out_row, out_col)` group, named from the *output* indices, expected size `OTS × OTS`, local origin `(out_col·OTS, out_row·OTS)` in the parent grid.

Filename parsing accepts 4-or-more digits (`tile_r(\d{4,})_c(\d{4,})`), matching Java's `%04d` widening beyond index 9999.

Units whose member compute tiles **all** appear in `_failures.json` are expected to be absent — reported as known-failed, never as missing. Configs whose tiles are externalized to a `tiles_file` make both checks return SKIPPED with an explicit message (the validator can't enumerate units without the inline tiles).

## `integrity` — zero-cost, default

One pass over ALL expected units (metadata reads are cheap; no sampling):

- File exists, or every member tile is journaled in `_failures.json`.
- TIFF magic bytes (`II`/`MM`) and size > 1 KB.
- With rasterio (`pip install datensee[validation]`): pixel dimensions match the unit, band count and dtype match the output config, CRS matches (pyproj-normalized), affine origin equals `parent translate + local origin px × scale` within 1e-6 (`scale_y` negative — NW-corner convention).
- On-disk files matching the tile-name pattern that map to no expected unit are flagged.

Without rasterio the check degrades to the existence/magic/size/accounting subset and appends a note to its message.

Catches: lost tiles, truncated writes, tiling-math and two-tier origin bugs, CRS confusion, dtype/band drift, phantom files.

## `pixels` — opt-in, costs EECUs

For up to `sample` (default 20) non-journaled compute tiles — chosen deterministically (first, last, evenly spaced; no seed) — re-fetch from the EE HV API in NPY format and compare against the on-disk data:

- The request grid is built exactly like the Java fetcher's `PixelGrid.forTile`: parent scale verbatim, translate shifted by the tile's pixel offset, the tile's own `width_px × height_px`.
- EE's structured NPY responses (one named field per band) are handled natively; **all bands** are compared, not just the first.
- In two-tier mode the on-disk side is a window of the output COG at the tile's block offset.
- Pass criteria: max absolute difference < 1e-4 over finite pixels.

Requires rasterio, EE API access, and a GEE project (defaults to the config's `gee_project`). Cost: roughly 1 EECU-second per sampled tile.

If `pixels` passes, the entire chain is correct: tiling, coordinate transforms, HV API requests, pixel decoding, GeoTIFF writing, and (in two-tier mode) block placement inside assembled COGs. Run it after pipeline changes or before trusting a large export; `integrity` alone is enough for routine post-export sanity.

## Exit codes

- `0` — all checks passed (SKIPPED counts as passing)
- `1` — one or more checks FAILED (or errored)
