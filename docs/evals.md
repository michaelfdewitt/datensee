# DatensEE Eval Catalog

Evals validate pipeline output correctness — are the pixels right, is the spatial metadata right, do tiles stitch together seamlessly?

## Usage

```bash
# Run all zero-cost evals against local output
datensee eval ./output --config config.json

# Run specific evals
datensee eval ./output --config config.json --evals E01,E03,E08

# Include E07 pixel accuracy (costs EECUs)
datensee eval ./output --config config.json --reference --gee-project my-project

# Machine-readable output
datensee eval ./output --config config.json --json report.json

# Auto-run evals after export
datensee export expr.json region.json -p my-project -o ./output --eval
```

```python
# Programmatic usage
from datensee.eval import validate_output, EvalID
from datensee.config import PipelineConfig

config = PipelineConfig.read_json("config.json")
report = validate_output("./output", config)
assert report.all_passed
```

## Sampling

Most evals don't check every tile. The default strategy is **stratified** (4 corners + random edges + random interior, N=20, seed=42 for reproducibility). Evals E01, E05, E08 always run on ALL tiles since they're just file existence / XML checks.

Strategies: `all`, `stratified` (default), `random`.

## Zero-Cost Evals

These read existing output only — no EE API calls required.

### E01 — Tile File Integrity

Every expected tile file exists, has TIFF magic bytes (`II` or `MM`), and is larger than 1 KB.

| | |
|---|---|
| **Runs on** | All tiles |
| **Pass criteria** | All non-failed tiles are valid TIFF files >1 KB |
| **Catches** | Missing tiles, corrupted downloads, truncated writes |

### E02 — Tile Dimensions

Tile pixel dimensions match `tile_size_pixels` from config. Band count and data type match the output config.

| | |
|---|---|
| **Runs on** | Sampled tiles |
| **Pass criteria** | Exact match for all sampled tiles |
| **Catches** | Tiling logic bugs, wrong band count, dtype mismatch |
| **Requires** | `rasterio` (`pip install datensee[eval]`) |

### E03 — Tile Geospatial Metadata

CRS matches config. Affine transform origin (translateX, translateY) matches tile coordinates (x_min, y_max).

| | |
|---|---|
| **Runs on** | Sampled tiles |
| **Pass criteria** | CRS match (pyproj-normalized), origin within 1e-6 |
| **Catches** | CRS misassignment, affine transform bugs, coordinate system confusion |
| **Requires** | `rasterio` |

### E04 — Boundary Continuity

Adjacent tiles' shared edge pixels form a smooth continuation. Reads the last column of the left tile and first column of the right tile (and analogously for top/bottom), computes mean absolute difference.

| | |
|---|---|
| **Runs on** | Sampled tiles (checks neighbors) |
| **Pass criteria** | Mean absolute difference < 50.0 (configurable) |
| **Catches** | Tile misalignment, off-by-one pixel shifts, grid origin bugs |
| **Requires** | `rasterio` |

### E05 — VRT Completeness

`mosaic.vrt` exists, parses as valid XML, references every tile in the config, has the correct band count, data type, and raster dimensions.

| | |
|---|---|
| **Runs on** | All tiles (VRT XML parsing only) |
| **Pass criteria** | All tiles referenced, metadata matches config |
| **Catches** | VRT generation bugs, missing tiles in mosaic |

### E06 — VRT Spatial Correctness

VRT bounding box (from GeoTransform + raster dimensions) covers the full extent of the tile grid.

| | |
|---|---|
| **Runs on** | VRT + config |
| **Pass criteria** | VRT bbox covers grid extent (half-pixel tolerance) |
| **Catches** | GeoTransform errors, wrong raster dimensions |

### E08 — Failure Accounting

`tiles_on_disk + tiles_in_failures == tiles_in_config`. Every tile must be accounted for — either as a file on disk or as an entry in `failures.json`.

| | |
|---|---|
| **Runs on** | All tiles |
| **Pass criteria** | Set equality on (row, col) |
| **Catches** | Silently lost tiles, phantom tiles not in config |

### E09 — Pixel Range Sanity

Sampled pixel values fall within expected range for the data type (e.g., uint8: [0, 255], float32: [-1e10, 1e10]).

| | |
|---|---|
| **Runs on** | Sampled tiles |
| **Pass criteria** | >95% of finite pixels in range, <5% all-NaN tiles |
| **Catches** | Overflow, garbage values, excessive nodata |
| **Requires** | `rasterio` |

### E10 — Output Size Plausibility

Total output size is within 0.2x–5x of the cost estimator's prediction.

| | |
|---|---|
| **Runs on** | All tiles (file size only) |
| **Pass criteria** | Within bounds |
| **Catches** | Empty tiles, excessive compression, missing data |

## API-Cost Eval

### E07 — Pixel Value Accuracy

The crown jewel. For sampled tiles, re-fetch the same tile from the EE HV API in NPY format and compare pixel values against the pipeline output.

| | |
|---|---|
| **Runs on** | Sampled tiles |
| **Pass criteria** | Max absolute difference < 1e-4 (epsilon) |
| **Catches** | Any pixel-level error in the entire pipeline chain |
| **Requires** | `rasterio`, EE API access, `--reference` flag |
| **Cost** | ~1 EECU-second per sampled tile |

If E07 passes, the entire chain is correct: tiling, coordinate transforms, HV API requests, pixel decoding, GeoTIFF writing.

## Exit Codes

- `0` — All evals passed (or skipped)
- `1` — One or more evals failed

## JSON Report Format

```json
{
  "output_path": "./output",
  "summary": {
    "passed": 8,
    "failed": 1,
    "total": 9
  },
  "results": [
    {
      "eval_id": "E01",
      "status": "passed",
      "message": "All 100 tiles are valid TIFF files",
      "details": {}
    }
  ]
}
```
