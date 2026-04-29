# DatensEE Validation Check Catalog

The validation suite is a post-export integration test that confirms output correctness — are the pixels right, is the spatial metadata right, do tiles stitch together seamlessly?

## Usage

```bash
# Run all zero-cost checks against local output
datensee validate ./output --config config.json

# Run specific checks
datensee validate ./output --config config.json --checks E01,E03,E08

# Include E07 pixel accuracy (costs EECUs)
datensee validate ./output --config config.json --reference --gee-project my-project

# Machine-readable output
datensee validate ./output --config config.json --json report.json

# Auto-run checks after export
datensee export expr.json region.json -p my-project -o ./output --validate
```

```python
# Programmatic usage
from datensee.validation import validate_output, CheckID
from datensee.config import PipelineConfig

config = PipelineConfig.read_json("config.json")
report = validate_output("./output", config)
assert report.all_passed
```

## Sampling

Most checks don't need every tile. The default strategy is **stratified** (4 corners + random edges + random interior, N=20, seed=42 for reproducibility). Checks E01 and E08 always run on ALL tiles since they're file-existence inspections.

Strategies: `all`, `stratified` (default), `random`.

## Zero-Cost Checks

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
| **Requires** | `rasterio` (`pip install datensee[validation]`) |

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

<!--
  E05 (VRT Completeness) and E06 (VRT Spatial Correctness) were removed
  when the pipeline stopped producing a VRT manifest. The check IDs are
  retired — output COGs are self-describing via standard GeoTIFF tags,
  so the per-tile checks (E01–E04) are now the spatial-correctness
  surface.
-->

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

## API-Cost Check

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

## Exit codes

- `0` — All checks passed (or skipped)
- `1` — One or more checks failed

## JSON report format

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
      "check_id": "E01",
      "status": "passed",
      "message": "All 100 tiles are valid TIFF files",
      "details": {}
    }
  ]
}
```
