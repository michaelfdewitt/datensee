"""Output validation integration tests — runs checks against a real EE export.

These tests require:
    - Application Default Credentials configured
    - The --integration flag and --gee-project option

Run with:
    cd cli && uv run pytest tests/test_validation_output_integration.py \
        --integration --gee-project=datensee-testing -v
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import httpx
import pytest

from datensee.auth import get_access_token
from datensee.config import OutputConfig, PipelineConfig, RunnerConfig
from datensee.tiling import decompose_region
from datensee.validation import CheckID, CheckStatus, validate_output
from datensee.validation.reference import _build_hv_request

# ---------------------------------------------------------------------------
# SRTM expression (reuse from test_integration_ee.py pattern)
# ---------------------------------------------------------------------------


def _srtm_elevation_expression() -> str:
    return json.dumps(
        {
            "result": "0",
            "values": {
                "0": {
                    "functionInvocationValue": {
                        "functionName": "Image.load",
                        "arguments": {
                            "id": {"constantValue": "USGS/SRTMGL1_003"},
                        },
                    }
                }
            },
        }
    )


_SF_BAY_SMALL = {
    "type": "Polygon",
    "coordinates": [
        [
            [-122.5, 37.75],
            [-122.25, 37.75],
            [-122.25, 38.0],
            [-122.5, 38.0],
            [-122.5, 37.75],
        ]
    ],
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def gee_project(request: pytest.FixtureRequest) -> str:
    project = request.config.getoption("--gee-project")
    if not project:
        pytest.skip("--gee-project not provided")
    return project


@pytest.fixture(scope="module")
def access_token() -> str:
    return get_access_token()


@pytest.fixture(scope="module")
def real_output(gee_project: str, access_token: str) -> tuple[Path, PipelineConfig]:
    """Fetch a small set of real tiles from EE and write them as GeoTIFFs.

    Returns (output_dir, config) for the validation check suite.
    """
    grid = decompose_region(
        _SF_BAY_SMALL,
        scale_meters=1000.0,
        crs="EPSG:4326",
        tile_size_pixels=64,
    )

    config = PipelineConfig(
        ee_expression=_srtm_elevation_expression(),
        gee_project=gee_project,
        tile_grid=grid,
        output=OutputConfig(output_path="/tmp/validation-test", band_count=1, data_type="float32"),
        runner=RunnerConfig(mode="local"),
    )

    output_dir = Path(tempfile.mkdtemp(prefix="datensee-validation-test-"))

    with httpx.Client(timeout=120.0) as client:
        for tile in grid.tiles:
            body = _build_hv_request(
                config.ee_expression,
                (tile.x_min, tile.y_min, tile.x_max, tile.y_max),
                grid.tile_size_pixels,
                grid.crs,
                file_format="GEO_TIFF",
            )
            url = f"https://earthengine-highvolume.googleapis.com/v1/projects/{gee_project}/image:computePixels"
            resp = client.post(
                url,
                json=body,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "x-goog-user-project": gee_project,
                },
            )
            assert resp.status_code == 200, f"HTTP {resp.status_code}: {resp.text[:200]}"

            tile_path = output_dir / f"tile_r{tile.row:04d}_c{tile.col:04d}.tif"
            tile_path.write_bytes(resp.content)

    # Also write VRT
    from datensee.assemble import write_vrt

    write_vrt(config, output_dir)

    return output_dir, config


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestEvalWithRealOutput:
    """Run validation checks against real EE tiles fetched in the fixture."""

    def test_zero_cost_evals_pass(self, real_output: tuple[Path, PipelineConfig]) -> None:
        """All zero-cost checks should pass on correctly-fetched tiles."""
        output_dir, config = real_output
        report = validate_output(
            output_dir,
            config,
            checks=[CheckID.E01, CheckID.E05, CheckID.E06, CheckID.E08, CheckID.E10],
        )
        for r in report.results:
            assert r.status in (CheckStatus.PASSED, CheckStatus.SKIPPED), (
                f"{r.check_id}: {r.status} — {r.message}"
            )

    def test_e07_pixel_accuracy(
        self,
        real_output: tuple[Path, PipelineConfig],
        gee_project: str,
        access_token: str,
    ) -> None:
        """E07 should pass — pipeline tiles match direct EE fetches."""
        output_dir, config = real_output

        report = validate_output(
            output_dir,
            config,
            checks=[CheckID.E07],
            sample_size=4,
            gee_project=gee_project,
            access_token=access_token,
        )
        e07 = report.results[0]
        assert e07.status == CheckStatus.PASSED, f"E07: {e07.message}\n{e07.details}"
