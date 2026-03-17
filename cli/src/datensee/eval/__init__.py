"""DatensEE eval system — validate pipeline output correctness.

Three ways to use:
    1. CLI: datensee eval ./output --config config.json
    2. pytest: test_eval_integration.py runs evals after real pipeline output
    3. Programmatic: from datensee.eval import validate_output
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from datensee.config import PipelineConfig
from datensee.eval.catalog import EvalID, zero_cost_evals
from datensee.eval.catalog import get_eval as get_eval  # noqa: PLC0414
from datensee.eval.catalog import list_evals as list_evals  # noqa: PLC0414
from datensee.eval.report import EvalReport, EvalResult, EvalStatus
from datensee.eval.sampling import SamplingStrategy, sample_tiles


def validate_output(
    output_path: Path | str,
    config: PipelineConfig,
    *,
    evals: list[EvalID] | None = None,
    sample_size: int = 20,
    seed: int = 42,
    gee_project: str | None = None,
    access_token: str | None = None,
) -> EvalReport:
    """Run evals against pipeline output and return a structured report.

    Args:
        output_path: Directory (local) or GCS prefix containing tile GeoTIFFs.
        config: Pipeline config that produced the output.
        evals: Eval IDs to run. Defaults to all zero-cost evals.
        sample_size: Max tiles for sampling-based evals.
        seed: RNG seed for deterministic sampling.
        gee_project: GCP project ID for E07 reference comparison.
        access_token: OAuth2 bearer token for E07. Auto-acquired if None and needed.

    Returns:
        EvalReport with per-eval results.
    """
    from datensee.eval.assembly import (
        eval_e05_vrt_completeness,
        eval_e08_failure_accounting,
        eval_e10_size_plausibility,
    )
    from datensee.eval.reference import eval_e07_pixel_value_accuracy
    from datensee.eval.spatial import (
        eval_e03_tile_geospatial_metadata,
        eval_e04_boundary_continuity,
        eval_e06_vrt_spatial_correctness,
    )
    from datensee.eval.tile_integrity import (
        eval_e01_tile_file_integrity,
        eval_e02_tile_dimensions,
        eval_e09_pixel_range_sanity,
    )

    output = Path(output_path) if isinstance(output_path, str) else output_path
    eval_ids = evals or zero_cost_evals()

    tiles = config.tile_grid.tiles or []
    sampled = sample_tiles(tiles, strategy=SamplingStrategy.STRATIFIED, n=sample_size, seed=seed)

    results: list[EvalResult] = []

    def _run_e07() -> EvalResult:
        project = gee_project or config.gee_project
        token = access_token
        if token is None:
            from datensee.auth import get_access_token

            token = get_access_token()
        return eval_e07_pixel_value_accuracy(
            output, config, sampled, gee_project=project, access_token=token
        )

    dispatch: dict[EvalID, Callable[[], EvalResult]] = {
        EvalID.E01: lambda: eval_e01_tile_file_integrity(output, tiles),
        EvalID.E02: lambda: eval_e02_tile_dimensions(output, config, sampled),
        EvalID.E03: lambda: eval_e03_tile_geospatial_metadata(output, config, sampled),
        EvalID.E04: lambda: eval_e04_boundary_continuity(output, config, sampled),
        EvalID.E05: lambda: eval_e05_vrt_completeness(output, config),
        EvalID.E06: lambda: eval_e06_vrt_spatial_correctness(output, config),
        EvalID.E07: _run_e07,
        EvalID.E08: lambda: eval_e08_failure_accounting(output, config),
        EvalID.E09: lambda: eval_e09_pixel_range_sanity(output, config, sampled),
        EvalID.E10: lambda: eval_e10_size_plausibility(output, config),
    }

    for eval_id in eval_ids:
        runner = dispatch.get(eval_id)
        if runner is None:
            results.append(
                EvalResult(
                    eval_id=eval_id,
                    status=EvalStatus.SKIPPED,
                    message=f"{eval_id.value} not yet implemented",
                )
            )
            continue
        try:
            results.append(runner())
        except Exception as exc:
            results.append(
                EvalResult(
                    eval_id=eval_id,
                    status=EvalStatus.ERROR,
                    message=f"Eval raised {type(exc).__name__}: {exc}",
                )
            )

    return EvalReport(results=results, output_path=str(output), config=config)
