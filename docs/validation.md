# Validation Reference

Reference for the `datensee validate` CLI command and Python validation API.

## Usage

```bash
# Structural integrity check (zero-cost)
datensee validate ./output --config config.json

# Integrity check + sampled Earth Engine pixel comparison
datensee validate ./output --config config.json --pixels --gee-project my-project

# Run automatically following a local export
datensee export expr.json region.json -p my-project -o ./output --validate
```

```python
from datensee.config import PipelineConfig
from datensee.pixel.validation import validate_output

config = PipelineConfig.read_json("config.json")
report = validate_output("./output", config, pixels=True, sample=20)
assert report.all_passed
for r in report.results:
    print(r.check_id, r.status, r.message)
```

`validate_output(output_path, config, *, pixels=False, sample=20, gee_project=None)`
returns a `ValidationReport` containing `all_passed` (boolean) and `results`
(a list of `CheckResult` instances with `check_id`, `status`, `message`, and
`details`).

## Output Units

Checks evaluate on-disk files mapped as **output units**
(`datensee.pixel.validation.units`):

- **One-COG-per-compute-tile mode (default):** One unit per compute tile named
  `tile_r{row:04d}_c{col:04d}.tif`, with expected dimensions
  `width_px × height_px`.
- **Two-tier mode (`output_tile_size_pixels` configured):** One unit per
  distinct `(out_row, out_col)` group named `tile_r{out_row:04d}_c{out_col:04d}.tif`,
  with dimensions `OTS × OTS` and local origin `(out_col * OTS, out_row * OTS)`
  in the parent grid.

Filename parsing matches `tile_r(\d{4,})_c(\d{4,})`, accommodating row and column
indices >= 10000.

Units whose member compute tiles all appear in `_failures.json` are counted as
documented failures rather than missing files. If tile coordinates were
externalized to a `tiles_file`, validation returns `SKIPPED` because unit
enumeration requires inline coordinates.

## `integrity` Check (Default, zero-cost)

Evaluates all expected output units without sampling:
- Confirms file existence or corresponding failure entries in `_failures.json`.
- Validates TIFF magic bytes (`II` / `MM`) and minimum file size (> 1 KB).
- With `rasterio` installed (`pip install 'datensee[validation]'`):
  - Validates pixel dimensions against unit specifications.
  - Validates band count and data type against `output` configuration.
  - Validates CRS equivalence (pyproj-normalized).
  - Validates affine origin alignment within a 1e-6 numerical tolerance.
- Identifies any unexpected tile-named TIFF files in the output directory.

Without `rasterio`, the check validates file presence, header magic bytes, and
journal accounting.

## `pixels` Check (Opt-in)

Re-fetches a deterministic sample of tiles (up to `sample`, default 20) from
the High Volume API in NPY format:
- Constructs request grids matching `PixelGrid.forTile`: identical parent scale,
  translated offsets, and unit pixel dimensions.
- Compares all bands against corresponding on-disk pixels (windowed into
  assembled COGs in two-tier mode).
- Pass criterion: maximum absolute difference < 1e-4 across finite pixel values.

Requires `rasterio`, Earth Engine credentials, and a valid GCP project ID.

## Exit Codes

- `0`: All executed checks passed (or skipped).
- `1`: One or more checks failed or encountered an error.
