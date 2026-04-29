"""DatensEE output validation — post-export integration test suite.

Runs structural, spatial, and pixel-level checks against pipeline output
(local or GCS). The check catalog (E01–E10) lives in ``catalog.py``.

Three ways to use:
    1. CLI: datensee validate ./output --config config.json
    2. pytest: tests/test_validation_output_integration.py runs checks
       after a real pipeline output
    3. Programmatic: from datensee.validation import validate_output
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from datensee.config import PipelineConfig
from datensee.validation.catalog import CheckID, zero_cost_checks
from datensee.validation.catalog import get_check as get_check  # noqa: PLC0414
from datensee.validation.catalog import list_checks as list_checks  # noqa: PLC0414
from datensee.validation.report import CheckResult, CheckStatus, ValidationReport
from datensee.validation.sampling import SamplingStrategy, sample_tiles


def validate_output(
    output_path: Path | str,
    config: PipelineConfig,
    *,
    checks: list[CheckID] | None = None,
    sample_size: int = 20,
    seed: int = 42,
    gee_project: str | None = None,
    access_token: str | None = None,
) -> ValidationReport:
    """Run output checks against pipeline output and return a structured report.

    Args:
        output_path: Directory (local) or GCS prefix containing tile GeoTIFFs.
        config: Pipeline config that produced the output.
        checks: Check IDs to run. Defaults to all zero-cost checks.
        sample_size: Max tiles for sampling-based checks.
        seed: RNG seed for deterministic sampling.
        gee_project: GCP project ID for E07 reference comparison.
        access_token: OAuth2 bearer token for E07. Auto-acquired if None and needed.

    Returns:
        ValidationReport with per-check results.
    """
    from datensee.validation.assembly import (
        check_e08_failure_accounting,
        check_e10_size_plausibility,
    )
    from datensee.validation.reference import check_e07_pixel_value_accuracy
    from datensee.validation.spatial import (
        check_e03_tile_geospatial_metadata,
        check_e04_boundary_continuity,
    )
    from datensee.validation.tile_integrity import (
        check_e01_tile_file_integrity,
        check_e02_tile_dimensions,
        check_e09_pixel_range_sanity,
    )

    output = Path(output_path) if isinstance(output_path, str) else output_path
    check_ids = checks or zero_cost_checks()

    tiles = config.tile_grid.tiles or []
    sampled = sample_tiles(tiles, strategy=SamplingStrategy.STRATIFIED, n=sample_size, seed=seed)

    results: list[CheckResult] = []

    def _run_e07() -> CheckResult:
        project = gee_project or config.gee_project
        token = access_token
        if token is None:
            from datensee.auth import get_access_token

            token = get_access_token()
        return check_e07_pixel_value_accuracy(
            output, config, sampled, gee_project=project, access_token=token
        )

    dispatch: dict[CheckID, Callable[[], CheckResult]] = {
        CheckID.E01: lambda: check_e01_tile_file_integrity(output, tiles),
        CheckID.E02: lambda: check_e02_tile_dimensions(output, config, sampled),
        CheckID.E03: lambda: check_e03_tile_geospatial_metadata(output, config, sampled),
        CheckID.E04: lambda: check_e04_boundary_continuity(output, config, sampled),
        CheckID.E07: _run_e07,
        CheckID.E08: lambda: check_e08_failure_accounting(output, config),
        CheckID.E09: lambda: check_e09_pixel_range_sanity(output, config, sampled),
        CheckID.E10: lambda: check_e10_size_plausibility(output, config),
    }

    for check_id in check_ids:
        runner = dispatch.get(check_id)
        if runner is None:
            results.append(
                CheckResult(
                    check_id=check_id,
                    status=CheckStatus.SKIPPED,
                    message=f"{check_id.value} not yet implemented",
                )
            )
            continue
        try:
            results.append(runner())
        except Exception as exc:
            results.append(
                CheckResult(
                    check_id=check_id,
                    status=CheckStatus.ERROR,
                    message=f"Check raised {type(exc).__name__}: {exc}",
                )
            )

    return ValidationReport(results=results, output_path=str(output), config=config)
