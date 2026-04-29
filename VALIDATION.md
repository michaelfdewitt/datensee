# DatensEE Output Validation

DatensEE parallelizes Google Earth Engine image exports across Cloud Dataflow workers. The pipeline decomposes a region into thousands of tiles, fetches each via the EE High Volume API, and writes COGs to GCS. Lots of moving parts. The validation suite is a post-export integration test that confirms the output is correct.

## Why a separate suite, not just unit tests

Unit tests validate the code paths. The validation suite validates the *output*. A unit test checks that `decompose_region()` returns the right tile coordinates; a validation check confirms the GeoTIFFs on disk actually have the right pixels at the right coordinates. This matters because:

- The pipeline spans two runtimes (Python CLI + Java Beam workers) with a JSON config contract between them. Unit tests can't easily cross that boundary.
- Tile fetches are non-deterministic at scale — rate limiting, retries, partial failures. The output needs to be validated holistically.
- Pixel-level correctness depends on the interaction of tiling, CRS transforms, affine parameters, and EE server-side evaluation. No single unit test covers this chain.

The check suite runs against any pipeline output — local or GCS, fresh or stale. It's a library (`datensee.validation`), a CLI command (`datensee validate`), and a pytest integration. You can validate a 3,000-tile Dataflow export the same way you validate a 4-tile local test run.

## Check catalog

10 checks. 9 are zero-cost (read existing output, no API calls). 1 requires re-fetching tiles from the EE HV API.

| ID | Name | What it validates | Pass criteria | Scope |
|----|------|-------------------|---------------|-------|
| **E01** | Tile File Integrity | Files exist, TIFF magic bytes, >1 KB | All tiles valid | All tiles |
| **E02** | Tile Dimensions | Width/height match `tile_size_pixels`, band count and dtype match config | Exact match | Sampled |
| **E03** | Geospatial Metadata | CRS matches config, affine origin matches tile coordinates | CRS equal, origin within 1e-6 | Sampled |
| **E04** | Boundary Continuity | Shared edge pixels between adjacent tiles are continuous | Mean abs diff < 50 | Sampled |
| **E05** | VRT Completeness | `mosaic.vrt` references every tile, correct bands/dtype/dimensions | All tiles in VRT | All tiles |
| **E06** | VRT Spatial Correctness | VRT bounding box covers full tile grid extent | BBox covers grid | Config only |
| **E07** | Pixel Value Accuracy | Re-fetch tile from EE in NPY, compare against on-disk GeoTIFF | Max abs diff < 1e-4 | Sampled |
| **E08** | Failure Accounting | `tiles_on_disk` + `tiles_in_failures` == `tiles_in_config` | Set equality on (row, col) | All tiles |
| **E09** | Pixel Range Sanity | Finite pixel values within data-type range | >95% in range, <5% all-NaN | Sampled |
| **E10** | Size Plausibility | Total output size within 0.2x-5x of cost estimator prediction | Within bounds | All tiles |

### E07 is the terminal check

If E07 passes, every other check is redundant. It re-fetches the exact same tile from the EE HV API and does a pixel-level comparison against the pipeline output. If the pixels match, the entire chain — tiling math, CRS transforms, HV API request construction, response decoding, GeoTIFF serialization — is correct. The other checks exist because E07 costs EECUs and requires API credentials. They provide fast, free signal for the common failure modes.

## Sampling

Checking every tile is unnecessary for most checks and expensive for E07. The default sampling strategy is **stratified**: 4 corner tiles + random edge tiles + random interior tiles, capped at N=20, seed=42 for deterministic reproducibility. Checks that only do file-existence or XML inspection (E01, E05, E08) always run on all tiles.

Three strategies available: `all`, `stratified` (default), `random`.

## Cost model

| Tier | Checks | Cost | When to run |
|------|--------|------|-------------|
| Zero-cost | E01–E06, E08–E10 | 0 | Every run |
| API-cost | E07 | ~1 EECU-second per sampled tile | Gated behind `--reference` |

At default sample size (N=20), E07 costs ~20 EECU-seconds (~0.006 EECU-hours). Negligible, but gated by default because it requires EE credentials and network access.

## Running the suite

**CLI:**
```bash
datensee validate ./output --config config.json                   # zero-cost checks
datensee validate ./output --config config.json --reference       # + E07
datensee validate ./output --config config.json --checks E01,E07  # specific checks
datensee validate ./output --config config.json --json report.json
```

**After export:**
```bash
datensee export expr.json region.json -p my-project -o ./out --validate
```

**Programmatic:**
```python
from datensee.validation import validate_output, CheckID
from datensee.config import PipelineConfig

config = PipelineConfig.read_json("config.json")
report = validate_output("./output", config, checks=[CheckID.E01, CheckID.E07])
assert report.all_passed
```

**pytest:**
```python
def test_export_passes_validation(export_output, export_config):
    report = validate_output(export_output, export_config)
    assert report.all_passed, report.render()
```

## Report format

CLI renders a Rich table. `--json` writes a machine-readable report:

```json
{
  "output_path": "./output",
  "summary": { "passed": 9, "failed": 1, "total": 10 },
  "results": [
    {
      "check_id": "E04",
      "status": "failed",
      "message": "2/15 tile boundaries exceed threshold=50",
      "details": {
        "discontinuities": [
          { "tile_a": "tile_r0003_c0007.tif", "tile_b": "tile_r0003_c0008.tif", "edge": "vertical", "mad": 127.4 }
        ]
      }
    }
  ]
}
```

Exit code 0 = all passed/skipped. Exit code 1 = any failure.

## Architecture

```
datensee.validation
├── __init__.py        validate_output() — top-level API, dispatches to check functions
├── catalog.py         CheckID enum, CheckDefinition model, registry of E01–E10
├── sampling.py        Tile sampling strategies (all, stratified, random)
├── report.py          CheckResult / ValidationReport models, Rich renderer, JSON serializer
├── tiff.py            rasterio wrapper for GeoTIFF metadata + pixel reading
├── tile_integrity.py  E01 (file integrity), E02 (dimensions), E09 (pixel range)
├── spatial.py         E03 (CRS/affine), E04 (boundary continuity), E06 (VRT bbox)
├── assembly.py        E05 (VRT completeness), E08 (failure accounting), E10 (size)
└── reference.py       E07 (pixel accuracy vs EE HV API re-fetch)
```

The catalog is the source of truth. The CLI command, programmatic API, and pytest integration all use the same `validate_output()` entrypoint, which dispatches to individual check functions. Each check returns a `CheckResult` with a status, message, and optional structured details. Results are aggregated into a `ValidationReport`.

## Failure modes each check catches

The suite is designed around the real failure modes of a distributed tile-fetch pipeline:

- **Tiling math bugs** (wrong grid origin, pixel size, tile extent) — caught by E02, E03, E04
- **CRS confusion** (EPSG:4326 vs EPSG:32632, axis order) — caught by E03, E06
- **Tile boundary discontinuities** (off-by-one, non-snapped grid) — caught by E04
- **Silent tile loss** (worker crash, timeout, unlogged failure) — caught by E01, E08
- **VRT assembly bugs** (wrong tile placement, missing references) — caught by E05, E06
- **Pixel corruption** (dtype mismatch, overflow, encoding error) — caught by E02, E09, E07
- **Systemic pipeline failures** (empty output, massive over/undershoot) — caught by E10
