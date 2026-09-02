# DatensEE Output Validation

DatensEE parallelizes Google Earth Engine image exports across Cloud Dataflow workers. The pipeline decomposes a region into thousands of tiles, fetches each via the EE High Volume API, and writes COGs to GCS. Lots of moving parts. The validation suite is a post-export integration test that confirms the output is correct.

Reference (usage, pass criteria, exit codes): [`docs/validation.md`](docs/validation.md).

## Why a separate suite, not just unit tests

Unit tests validate the code paths. The validation suite validates the *output*. A unit test checks that `decompose_region()` returns the right tile coordinates; a validation check confirms the GeoTIFFs on disk actually have the right pixels at the right coordinates. The pipeline spans two runtimes (Python CLI + Java Beam workers) with a JSON config contract between them, and pixel-level correctness depends on the interaction of tiling, CRS transforms, affine parameters, and EE server-side evaluation — no unit test crosses that whole chain.

## The two checks

**`integrity`** (zero-cost, always runs). One pass over every expected output unit — the files the pipeline actually writes: one COG per compute tile by default, one per `(out_row, out_col)` group in two-tier mode. For each unit: the file exists (or every member compute tile is journaled in `_failures.json`), has TIFF magic bytes and a plausible size, and — via rasterio — the expected dimensions, band count, dtype, CRS, and affine origin (`parent translate + local origin × scale`). On-disk tile-named files that map to no expected unit are flagged. Metadata reads are cheap, so it runs on all units — no sampling. Without rasterio it degrades to the existence/magic/size/accounting subset and says so in its message.

**`pixels`** (opt-in, costs EECUs). The terminal check: for sampled non-journaled compute tiles (deterministic first/last/evenly-spaced, default 20), re-fetch the same tile from the EE HV API in NPY format — building the request grid exactly the way the Java fetcher does — and compare every band against the pipeline output, windowing into assembled COGs in two-tier mode. If the pixels match, the entire chain — tiling math, CRS transforms, HV API request construction, response decoding, GeoTIFF serialization, block placement — is correct. `integrity` exists because `pixels` costs EECUs and requires API credentials; it provides fast, free signal for the common failure modes (lost tiles, wrong dimensions, misplaced origins, dtype drift).

## Design invariants

- **Output units, not compute tiles.** The suite validates the files the pipeline writes; the config → unit mapping lives in one place (`pixel/validation/units.py`) so checks can't drift from the writer's naming.
- **`_failures.json` is the accounting boundary.** Dead-lettered tiles are expected to be absent: units whose members all failed count as known-failed, not missing, and journaled tiles are excluded from the `pixels` sample.
- **Externalized tile lists skip loudly.** A `tiles_file` config makes both checks return SKIPPED with an explicit message instead of guessing.
- **No false passes.** A check that compared nothing returns SKIPPED, never PASSED; a missing or unreadable expected file is a failure.

## Running it

```python
from datensee.pixel.validation import validate_output
from datensee.config import PipelineConfig

config = PipelineConfig.read_json("config.json")
report = validate_output("./output", config)                 # integrity only
report = validate_output("./output", config, pixels=True)    # + EE re-fetch
assert report.all_passed
```

CLI: `datensee validate ./output --config config.json` (exit 0 = all passed, 1 = failures); `datensee export ... --validate` runs integrity after a local export.

## Architecture

```
datensee.pixel.validation
├── __init__.py   validate_output(), CheckResult/ValidationReport models, integrity check
├── units.py      Output-unit mapping (config → expected files/origins/sizes), journal reader
└── pixels.py     pixels check (EE HV API re-fetch + all-band comparison)
```
