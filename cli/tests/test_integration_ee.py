"""Integration tests against the Earth Engine High Volume API.

These tests send real HTTP requests to the EE HV endpoint. They use
SRTM elevation data with lightweight math (rescaling, gradient, multi-band
stacking) so that tiles have spatially varying values: tiling misalignment,
CRS errors, or projection bugs produce visibly wrong results. SRTM is
pre-cached on EE servers and consumes negligible EECU time.

Run with:
    uv run pytest tests/test_integration_ee.py --integration --gee-project=datensee-testing -v

Requirements:
    - Application Default Credentials configured (`gcloud auth application-default login`)
    - The project must have the Earth Engine API enabled and be registered for use
"""

from __future__ import annotations

import io
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import httpx
import numpy as np
import pytest

from datensee.auth import get_access_token
from datensee.pixel.tiling import _pixel_size_native, decompose_region, tile_bbox

HV_ENDPOINT = (
    "https://earthengine-highvolume.googleapis.com/v1/projects/{project}/image:computePixels"
)


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
def hv_client() -> httpx.Client:
    client = httpx.Client(timeout=120.0)
    yield client
    client.close()


# ---------------------------------------------------------------------------
# Expression builders: SRTM-based, spatially varying, cheap to compute.
#
# All expressions use the SRTM 30m DEM (NASA/USGS). It's pre-cached on EE
# servers so evaluation is fast. The math applied is trivial (divide, multiply,
# subtract) but the spatial variation in elevation means any tiling or CRS
# bugs will produce visible discontinuities at tile boundaries.
# ---------------------------------------------------------------------------


def _srtm_elevation_expression() -> str:
    """SRTM elevation in meters (single band, spatially varying).

    Equivalent to: ee.Image('USGS/SRTMGL1_003')
    """
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


def _srtm_rescaled_expression() -> str:
    """SRTM elevation rescaled to 0–1 range: (elevation - 0) / 9000.

    Equivalent to: ee.Image('USGS/SRTMGL1_003').divide(9000)

    Exercises server-side division. Values will be ~0 at sea level,
    ~0.5 for Himalayan peaks. Spatially varying everywhere.
    """
    return json.dumps(
        {
            "result": "0",
            "values": {
                "0": {
                    "functionInvocationValue": {
                        "functionName": "Image.divide",
                        "arguments": {
                            "image1": {
                                "functionInvocationValue": {
                                    "functionName": "Image.load",
                                    "arguments": {
                                        "id": {"constantValue": "USGS/SRTMGL1_003"},
                                    },
                                }
                            },
                            "image2": {
                                "functionInvocationValue": {
                                    "functionName": "Image.constant",
                                    "arguments": {
                                        "value": {"constantValue": 9000},
                                    },
                                }
                            },
                        },
                    }
                }
            },
        }
    )


def _srtm_multiband_expression() -> str:
    """3-band image: [elevation, elevation/9000, elevation*elevation/1e6].

    Equivalent to:
        dem = ee.Image('USGS/SRTMGL1_003')
        ee.Image.cat([dem, dem.divide(9000), dem.multiply(dem).divide(1e6)])

    Each band has different numeric range but all are spatially varying.
    Multi-band tiling bugs (band ordering, band count) will be caught.
    """
    srtm_load = {
        "functionInvocationValue": {
            "functionName": "Image.load",
            "arguments": {
                "id": {"constantValue": "USGS/SRTMGL1_003"},
            },
        }
    }
    srtm_scaled = {
        "functionInvocationValue": {
            "functionName": "Image.divide",
            "arguments": {
                "image1": srtm_load,
                "image2": {
                    "functionInvocationValue": {
                        "functionName": "Image.constant",
                        "arguments": {"value": {"constantValue": 9000}},
                    }
                },
            },
        }
    }
    srtm_squared = {
        "functionInvocationValue": {
            "functionName": "Image.divide",
            "arguments": {
                "image1": {
                    "functionInvocationValue": {
                        "functionName": "Image.multiply",
                        "arguments": {
                            "image1": srtm_load,
                            "image2": srtm_load,
                        },
                    }
                },
                "image2": {
                    "functionInvocationValue": {
                        "functionName": "Image.constant",
                        "arguments": {"value": {"constantValue": 1_000_000}},
                    }
                },
            },
        }
    }
    return json.dumps(
        {
            "result": "0",
            "values": {
                "0": {
                    "functionInvocationValue": {
                        "functionName": "Image.cat",
                        "arguments": {
                            "images": {
                                "arrayValue": {"values": [srtm_load, srtm_scaled, srtm_squared]}
                            }
                        },
                    }
                }
            },
        }
    )


def _srtm_slope_expression() -> str:
    """Terrain slope from SRTM (uses ee.Terrain.slope()).

    Equivalent to: ee.Terrain.slope(ee.Image('USGS/SRTMGL1_003'))

    Slope is computed from the local elevation gradient. Highly spatially
    varying (flat plains ≈ 0°, mountain ridges ≈ 40°+). Any pixel
    misalignment will produce wrong slope values at tile edges.
    """
    return json.dumps(
        {
            "result": "0",
            "values": {
                "0": {
                    "functionInvocationValue": {
                        "functionName": "Terrain.slope",
                        "arguments": {
                            "input": {
                                "functionInvocationValue": {
                                    "functionName": "Image.load",
                                    "arguments": {
                                        "id": {"constantValue": "USGS/SRTMGL1_003"},
                                    },
                                }
                            }
                        },
                    }
                }
            },
        }
    )


# ---------------------------------------------------------------------------
# HV API request helper
# ---------------------------------------------------------------------------


def _build_hv_request(
    expression: str,
    tile_bounds: tuple[float, float, float, float],
    tile_size: int,
    crs: str,
    file_format: str = "GEO_TIFF",
) -> dict[str, Any]:
    """Build a computePixels request body."""
    x_min, y_min, x_max, y_max = tile_bounds
    pixel_w = (x_max - x_min) / tile_size
    pixel_h = (y_max - y_min) / tile_size

    return {
        "expression": json.loads(expression),
        "fileFormat": file_format,
        "grid": {
            "dimensions": {"width": tile_size, "height": tile_size},
            "affineTransform": {
                "scaleX": pixel_w,
                "shearX": 0,
                "translateX": x_min,
                "shearY": 0,
                "scaleY": -pixel_h,
                "translateY": y_max,
            },
            "crsCode": crs,
        },
    }


def _fetch_tile(
    client: httpx.Client,
    project: str,
    token: str,
    request_body: dict[str, Any],
) -> httpx.Response:
    """Send a single computePixels request."""
    url = HV_ENDPOINT.format(project=project)
    return client.post(
        url,
        json=request_body,
        headers={
            "Authorization": f"Bearer {token}",
            "x-goog-user-project": project,
        },
    )


def _assert_valid_geotiff(content: bytes, min_size: int = 100) -> None:
    """Assert the response bytes look like a valid GeoTIFF."""
    assert content[:2] in (b"II", b"MM"), "Response is not a TIFF (wrong magic bytes)"
    assert len(content) > min_size, f"GeoTIFF too small ({len(content)} bytes)"


# ---------------------------------------------------------------------------
# Regions
# ---------------------------------------------------------------------------

# Small: SF Bay Area, 0.25° × 0.25° (a few tiles at moderate scale).
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

# Medium: Sierra Nevada + Central Valley (varied terrain, ~1° × 1°).
_SIERRA_NEVADA = {
    "type": "Polygon",
    "coordinates": [
        [
            [-120.5, 37.0],
            [-119.0, 37.0],
            [-119.0, 38.0],
            [-120.5, 38.0],
            [-120.5, 37.0],
        ]
    ],
}


# Large: California statewide (rough bbox) (for scale testing).
_CALIFORNIA = {
    "type": "Polygon",
    "coordinates": [
        [
            [-124.4, 32.5],
            [-114.1, 32.5],
            [-114.1, 42.0],
            [-124.4, 42.0],
            [-124.4, 32.5],
        ]
    ],
}


# ---------------------------------------------------------------------------
# Tests: Basic ComputePixels
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestComputePixelsBasic:
    """Validate that the HV API returns valid GeoTIFFs for various expressions."""

    def test_srtm_elevation_wgs84(
        self, gee_project: str, access_token: str, hv_client: httpx.Client
    ) -> None:
        """SRTM elevation in EPSG:4326."""
        body = _build_hv_request(
            _srtm_elevation_expression(),
            tile_bounds=(-122.5, 37.5, -122.0, 38.0),
            tile_size=64,
            crs="EPSG:4326",
        )
        resp = _fetch_tile(hv_client, gee_project, access_token, body)
        assert resp.status_code == 200, f"HTTP {resp.status_code}: {resp.text[:200]}"
        _assert_valid_geotiff(resp.content)

    def test_srtm_elevation_utm(
        self, gee_project: str, access_token: str, hv_client: httpx.Client
    ) -> None:
        """SRTM in UTM Zone 10N (verifies projected CRS)."""
        body = _build_hv_request(
            _srtm_elevation_expression(),
            tile_bounds=(545000, 4176000, 547560, 4178560),
            tile_size=64,
            crs="EPSG:32610",
        )
        resp = _fetch_tile(hv_client, gee_project, access_token, body)
        assert resp.status_code == 200, f"HTTP {resp.status_code}: {resp.text[:200]}"
        _assert_valid_geotiff(resp.content)

    def test_srtm_rescaled(
        self, gee_project: str, access_token: str, hv_client: httpx.Client
    ) -> None:
        """Rescaled elevation (0-1) via server-side division."""
        body = _build_hv_request(
            _srtm_rescaled_expression(),
            tile_bounds=(-120.0, 37.5, -119.5, 38.0),
            tile_size=64,
            crs="EPSG:4326",
        )
        resp = _fetch_tile(hv_client, gee_project, access_token, body)
        assert resp.status_code == 200
        _assert_valid_geotiff(resp.content)

    def test_srtm_slope(self, gee_project: str, access_token: str, hv_client: httpx.Client) -> None:
        """Terrain slope derived from elevation gradient (highly spatial)."""
        body = _build_hv_request(
            _srtm_slope_expression(),
            tile_bounds=(-120.0, 37.5, -119.5, 38.0),
            tile_size=64,
            crs="EPSG:4326",
        )
        resp = _fetch_tile(hv_client, gee_project, access_token, body)
        assert resp.status_code == 200
        _assert_valid_geotiff(resp.content)

    def test_srtm_multiband(
        self, gee_project: str, access_token: str, hv_client: httpx.Client
    ) -> None:
        """3-band SRTM derivative (validates multi-band fetch)."""
        body = _build_hv_request(
            _srtm_multiband_expression(),
            tile_bounds=(-120.0, 37.5, -119.5, 38.0),
            tile_size=64,
            crs="EPSG:4326",
        )
        resp = _fetch_tile(hv_client, gee_project, access_token, body)
        assert resp.status_code == 200
        _assert_valid_geotiff(resp.content, min_size=500)

    def test_srtm_slope_utm(
        self, gee_project: str, access_token: str, hv_client: httpx.Client
    ) -> None:
        """Slope in UTM (combines derived band with projected CRS)."""
        body = _build_hv_request(
            _srtm_slope_expression(),
            tile_bounds=(545000, 4176000, 547560, 4178560),
            tile_size=64,
            crs="EPSG:32610",
        )
        resp = _fetch_tile(hv_client, gee_project, access_token, body)
        assert resp.status_code == 200
        _assert_valid_geotiff(resp.content)


@pytest.mark.integration
class TestTilingAndFetch:
    """Verify our tiling logic produces grids the HV API accepts.

    These tile a region using decompose_region(), then fetch every tile.
    If tiling produces coordinates that don't align with the requested
    CRS or that overlap/gap incorrectly, the HV API will reject them
    or return garbled results.
    """

    def test_tiled_srtm_epsg4326(
        self, gee_project: str, access_token: str, hv_client: httpx.Client
    ) -> None:
        """Tile Sierra Nevada in WGS84, fetch elevation for every tile."""
        grid = decompose_region(_SIERRA_NEVADA, scale_meters=1000.0, crs="EPSG:4326")
        assert len(grid.tiles) >= 1

        for tile in grid.tiles:
            body = _build_hv_request(
                _srtm_elevation_expression(),
                tile_bounds=tile_bbox(grid.pixel_grid, tile),
                tile_size=grid.tile_size_pixels,
                crs=grid.crs,
            )
            resp = _fetch_tile(hv_client, gee_project, access_token, body)
            assert resp.status_code == 200, (
                f"Tile r={tile.row},c={tile.col} failed: HTTP {resp.status_code}"
            )
            _assert_valid_geotiff(resp.content)

    def test_tiled_srtm_utm(
        self, gee_project: str, access_token: str, hv_client: httpx.Client
    ) -> None:
        """Tile SF Bay in UTM Zone 10N, fetch slope for every tile."""
        grid = decompose_region(
            _SF_BAY_SMALL,
            scale_meters=1000.0,
            crs="EPSG:32610",
            tile_size_pixels=64,
        )
        assert len(grid.tiles) >= 1

        for tile in grid.tiles:
            body = _build_hv_request(
                _srtm_slope_expression(),
                tile_bounds=tile_bbox(grid.pixel_grid, tile),
                tile_size=grid.tile_size_pixels,
                crs=grid.crs,
            )
            resp = _fetch_tile(hv_client, gee_project, access_token, body)
            assert resp.status_code == 200, (
                f"Tile r={tile.row},c={tile.col} failed: HTTP {resp.status_code}"
            )

    def test_tiled_multiband_epsg4326(
        self, gee_project: str, access_token: str, hv_client: httpx.Client
    ) -> None:
        """Tile SF Bay, fetch 3-band SRTM derivative for every tile."""
        grid = decompose_region(
            _SF_BAY_SMALL, scale_meters=1000.0, crs="EPSG:4326", tile_size_pixels=64
        )

        for tile in grid.tiles:
            body = _build_hv_request(
                _srtm_multiband_expression(),
                tile_bounds=tile_bbox(grid.pixel_grid, tile),
                tile_size=grid.tile_size_pixels,
                crs=grid.crs,
            )
            resp = _fetch_tile(hv_client, gee_project, access_token, body)
            assert resp.status_code == 200, (
                f"Tile r={tile.row},c={tile.col} failed: HTTP {resp.status_code}"
            )


@pytest.mark.integration
class TestHighVolumeBatch:
    """Send thousands of requests to validate throughput and error rates.

    Uses SRTM elevation and slope; each tile is spatially varying and
    will expose any systematic tiling or CRS issues across a large grid.
    EECU cost is still minimal because SRTM is pre-cached and the math
    is trivial.
    """

    def _fetch_many_tiles(
        self,
        client: httpx.Client,
        project: str,
        token: str,
        expression: str,
        grid_crs: str,
        parent_pixel_grid: Any,
        tiles: list,
        tile_size: int,
        max_workers: int = 16,
    ) -> tuple[int, int, list[str], float]:
        """Fetch tiles concurrently. Returns (successes, failures, error_msgs, elapsed)."""
        successes = 0
        failures = 0
        error_msgs: list[str] = []
        start = time.monotonic()

        def fetch_one(tile: Any) -> tuple[int, str]:
            body = _build_hv_request(
                expression,
                tile_bounds=tile_bbox(parent_pixel_grid, tile),
                tile_size=tile_size,
                crs=grid_crs,
            )
            resp = _fetch_tile(client, project, token, body)
            detail = ""
            if resp.status_code != 200:
                detail = f"r={tile.row},c={tile.col} HTTP {resp.status_code}: {resp.text[:100]}"
            return resp.status_code, detail

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(fetch_one, t): t for t in tiles}
            for future in as_completed(futures):
                status, detail = future.result()
                if status == 200:
                    successes += 1
                else:
                    failures += 1
                    if detail:
                        error_msgs.append(detail)

        elapsed = time.monotonic() - start
        return successes, failures, error_msgs, elapsed

    def test_1000_tiles_srtm_elevation(
        self, gee_project: str, access_token: str, hv_client: httpx.Client
    ) -> None:
        """Fetch ~1000 tiles of SRTM elevation over California.

        At 500m scale with 16×16 tiles, each tile covers 8km × 8km.
        Elevation varies dramatically across California (Death Valley to
        Mt Whitney) so any systematic tiling issue will produce errors.
        """
        grid = decompose_region(
            _CALIFORNIA, scale_meters=500.0, crs="EPSG:4326", tile_size_pixels=16
        )
        tiles = grid.tiles[:1000]
        assert len(tiles) >= 500, f"Expected ≥500 tiles, got {len(tiles)}"

        successes, failures, errors, elapsed = self._fetch_many_tiles(
            hv_client,
            gee_project,
            access_token,
            _srtm_elevation_expression(),
            grid.crs,
            grid.pixel_grid,
            tiles,
            grid.tile_size_pixels,
            max_workers=16,
        )

        failure_rate = failures / len(tiles) if tiles else 0
        assert failure_rate < 0.05, (
            f"Too many failures: {failures}/{len(tiles)} "
            f"({failure_rate:.1%}) in {elapsed:.1f}s. "
            f"First errors: {errors[:5]}"
        )
        assert successes > 0

    def test_2000_tiles_srtm_multiband(
        self, gee_project: str, access_token: str, hv_client: httpx.Client
    ) -> None:
        """Fetch ~2000 tiles of 3-band SRTM derivative.

        Multi-band + spatially varying = catches both band-ordering bugs
        and tiling misalignment simultaneously.
        """
        grid = decompose_region(
            _CALIFORNIA, scale_meters=500.0, crs="EPSG:4326", tile_size_pixels=16
        )
        tiles = grid.tiles[:2000]
        assert len(tiles) >= 1000, f"Expected ≥1000 tiles, got {len(tiles)}"

        successes, failures, errors, elapsed = self._fetch_many_tiles(
            hv_client,
            gee_project,
            access_token,
            _srtm_multiband_expression(),
            grid.crs,
            grid.pixel_grid,
            tiles,
            grid.tile_size_pixels,
            max_workers=16,
        )

        failure_rate = failures / len(tiles) if tiles else 0
        assert failure_rate < 0.05, (
            f"Too many failures: {failures}/{len(tiles)} "
            f"({failure_rate:.1%}) in {elapsed:.1f}s. "
            f"First errors: {errors[:5]}"
        )

    def test_1000_tiles_srtm_slope_utm(
        self, gee_project: str, access_token: str, hv_client: httpx.Client
    ) -> None:
        """Fetch ~1000 tiles of slope in UTM (hardest CRS + derived band combo).

        Slope is computed from the local elevation gradient, so if the CRS
        transform is wrong, EE will compute slope from the wrong neighborhood
        and return incorrect values or errors.
        """
        grid = decompose_region(
            _CALIFORNIA, scale_meters=500.0, crs="EPSG:32610", tile_size_pixels=16
        )
        tiles = grid.tiles[:1000]
        assert len(tiles) >= 200, f"Expected ≥200 tiles, got {len(tiles)}"

        successes, failures, errors, elapsed = self._fetch_many_tiles(
            hv_client,
            gee_project,
            access_token,
            _srtm_slope_expression(),
            grid.crs,
            grid.pixel_grid,
            tiles,
            grid.tile_size_pixels,
            max_workers=16,
        )

        failure_rate = failures / len(tiles) if tiles else 0
        assert failure_rate < 0.05, (
            f"Too many failures: {failures}/{len(tiles)} "
            f"({failure_rate:.1%}) in {elapsed:.1f}s. "
            f"First errors: {errors[:5]}"
        )


@pytest.mark.integration
class TestEndToEndConfigRoundtrip:
    """Full Python-side pipeline: config → tiling → fetch.

    Exercises config construction, tiling, JSON serialization roundtrip,
    and fetch, as the export command would do before handing off to Java.
    """

    def test_config_produces_fetchable_tiles_epsg4326(
        self, gee_project: str, access_token: str, hv_client: httpx.Client
    ) -> None:
        """Build PipelineConfig with SRTM slope, verify every tile fetches."""
        from datensee.config import OutputConfig, PipelineConfig, RunnerConfig

        grid = decompose_region(
            _SF_BAY_SMALL, scale_meters=500.0, crs="EPSG:4326", tile_size_pixels=64
        )
        config = PipelineConfig(
            ee_expression=_srtm_slope_expression(),
            gee_project=gee_project,
            tile_grid=grid,
            output=OutputConfig(output_path="/tmp/test-output"),
            runner=RunnerConfig(mode="local"),
        )

        assert config.tile_grid.crs == "EPSG:4326"
        assert len(config.tile_grid.tiles) >= 1

        for tile in config.tile_grid.tiles:
            body = _build_hv_request(
                config.ee_expression,
                tile_bounds=tile_bbox(grid.pixel_grid, tile),
                tile_size=config.tile_grid.tile_size_pixels,
                crs=config.tile_grid.crs,
            )
            resp = _fetch_tile(hv_client, gee_project, access_token, body)
            assert resp.status_code == 200, (
                f"Tile r={tile.row},c={tile.col}: HTTP {resp.status_code}"
            )
            _assert_valid_geotiff(resp.content)

    def test_config_produces_fetchable_tiles_utm(
        self, gee_project: str, access_token: str, hv_client: httpx.Client
    ) -> None:
        """PipelineConfig with 3-band SRTM in UTM (the M2 happy path)."""
        from datensee.config import OutputConfig, PipelineConfig, RunnerConfig

        grid = decompose_region(
            _SF_BAY_SMALL, scale_meters=500.0, crs="EPSG:32610", tile_size_pixels=64
        )
        config = PipelineConfig(
            ee_expression=_srtm_multiband_expression(),
            gee_project=gee_project,
            tile_grid=grid,
            output=OutputConfig(
                output_path="/tmp/test-output",
                band_count=3,
                data_type="float32",
            ),
            runner=RunnerConfig(mode="local"),
        )

        assert config.tile_grid.crs == "EPSG:32610"

        for tile in config.tile_grid.tiles:
            body = _build_hv_request(
                config.ee_expression,
                tile_bounds=tile_bbox(grid.pixel_grid, tile),
                tile_size=config.tile_grid.tile_size_pixels,
                crs=config.tile_grid.crs,
            )
            resp = _fetch_tile(hv_client, gee_project, access_token, body)
            assert resp.status_code == 200, (
                f"Tile r={tile.row},c={tile.col}: HTTP {resp.status_code}"
            )

    def test_config_json_roundtrip_then_fetch(
        self,
        gee_project: str,
        access_token: str,
        hv_client: httpx.Client,
        tmp_path: Any,
    ) -> None:
        """Serialize config to JSON, read it back, fetch from restored config.

        Mimics the CLI→Java handoff: Python writes config JSON, Java reads
        it and builds HV API requests from the deserialized fields.
        """
        from datensee.config import OutputConfig, PipelineConfig, RunnerConfig

        grid = decompose_region(
            _SF_BAY_SMALL, scale_meters=2000.0, crs="EPSG:4326", tile_size_pixels=32
        )
        config = PipelineConfig(
            ee_expression=_srtm_rescaled_expression(),
            gee_project=gee_project,
            tile_grid=grid,
            output=OutputConfig(output_path="/tmp/test"),
            runner=RunnerConfig(mode="local"),
        )

        config_path = tmp_path / "config.json"
        config.write_json(config_path)
        restored = PipelineConfig.read_json(config_path)

        assert restored.tile_grid.crs == config.tile_grid.crs
        assert len(restored.tile_grid.tiles) == len(config.tile_grid.tiles)
        assert restored.ee_expression == config.ee_expression

        for tile in restored.tile_grid.tiles:
            body = _build_hv_request(
                restored.ee_expression,
                tile_bounds=tile_bbox(restored.tile_grid.pixel_grid, tile),
                tile_size=restored.tile_grid.tile_size_pixels,
                crs=restored.tile_grid.crs,
            )
            resp = _fetch_tile(hv_client, gee_project, access_token, body)
            assert resp.status_code == 200
            _assert_valid_geotiff(resp.content)


# ---------------------------------------------------------------------------
# Pixel alignment helper
# ---------------------------------------------------------------------------


def _fetch_tile_as_numpy(
    client: httpx.Client,
    project: str,
    token: str,
    expression: str,
    tile_bounds: tuple[float, float, float, float],
    tile_size: int,
    crs: str,
) -> np.ndarray:
    """Fetch a tile and decode the GeoTIFF into a numpy array.

    Uses NPY format via the HV API to avoid needing GDAL/rasterio for
    decoding. Falls back to raw GeoTIFF bytes if NPY isn't available.
    """
    body = _build_hv_request(expression, tile_bounds, tile_size, crs, "NPY")
    resp = _fetch_tile(client, project, token, body)
    assert resp.status_code == 200, f"HTTP {resp.status_code}: {resp.text[:200]}"
    return np.load(io.BytesIO(resp.content))


@pytest.mark.integration
class TestPixelAlignment:
    """Verify pixel-level alignment by fetching overlapping regions.

    Strategy: take a reference region, shift it by N pixels in the target
    CRS, tile both regions, and find tiles that overlap. Fetch the same
    physical area from both grids and compare the overlapping pixels.
    If the grid is properly snapped to global (0,0), the tiles from both
    grids land on the exact same pixel boundaries, so the N-pixel offset
    produces pixel-identical overlap.

    If the grid were anchored to the bbox (the old bug), the two grids
    would have different origins and the pixels wouldn't line up at all.
    """

    def _compare_shifted_grids(
        self,
        client: httpx.Client,
        project: str,
        token: str,
        expression: str,
        crs: str,
        scale_meters: float,
        tile_size_pixels: int,
        shift_pixels: int,
    ) -> None:
        """Shift a region by N pixels, fetch overlapping tiles, compare pixels.

        The reference region is a box over Sierra Nevada. We shift it by
        `shift_pixels` in the x direction (easting) and compare pixels
        in the overlap area.
        """
        # By design: pixel size is single-axis and latitude-independent; for
        # geographic CRSs we go through the equator constant; for projected
        # CRSs scale_meters is the literal pixel size in CRS units.
        pixel_native_x = _pixel_size_native(crs, scale_meters)
        shift_native = shift_pixels * pixel_native_x

        # Two overlapping WGS84 regions. The shift is applied in native
        # CRS units, but converted back to WGS84 for the GeoJSON input
        # since decompose_region always expects WGS84.
        if crs == "EPSG:4326":
            # Sierra Nevada area: direct WGS84.
            base_x, base_y = -120.5, 37.0
            width, height = 0.5, 0.5
            shift_x_wgs84 = shift_native
        else:
            # For projected CRS, shift in WGS84 degrees approximately.
            # We use a small SF Bay area. The shift is in native CRS units
            # but we need WGS84 coords for the input regions. We use two
            # slightly offset WGS84 regions; the key is that after
            # reprojection, the snapped grid produces the same tile origins.
            base_x, base_y = -122.3, 37.7
            width, height = 0.3, 0.3
            # Convert native shift to approximate WGS84 degrees.
            # At ~38°N, 1° longitude ≈ 87,800m.
            shift_x_wgs84 = shift_native / 87_800.0

        region_a = {
            "type": "Polygon",
            "coordinates": [
                [
                    [base_x, base_y],
                    [base_x + width, base_y],
                    [base_x + width, base_y + height],
                    [base_x, base_y + height],
                    [base_x, base_y],
                ]
            ],
        }
        region_b = {
            "type": "Polygon",
            "coordinates": [
                [
                    [base_x + shift_x_wgs84, base_y],
                    [base_x + width + shift_x_wgs84, base_y],
                    [base_x + width + shift_x_wgs84, base_y + height],
                    [base_x + shift_x_wgs84, base_y + height],
                    [base_x + shift_x_wgs84, base_y],
                ]
            ],
        }

        grid_a = decompose_region(
            region_a, scale_meters=scale_meters, crs=crs, tile_size_pixels=tile_size_pixels
        )
        grid_b = decompose_region(
            region_b, scale_meters=scale_meters, crs=crs, tile_size_pixels=tile_size_pixels
        )

        # Find tiles present in both grids (same snapped CRS origin). Tile
        # offsets are local to each parent grid, so we have to compose with
        # the parent's translate to get an absolute identifier.
        bbox_a = {tile_bbox(grid_a.pixel_grid, t): t for t in grid_a.tiles}
        bbox_b = {tile_bbox(grid_b.pixel_grid, t): t for t in grid_b.tiles}
        # Round to 6 decimals so floating-point round-trips don't fail an
        # otherwise-perfect snap.
        keys_a = {tuple(round(v, 6) for v in k): t for k, t in bbox_a.items()}
        keys_b = {tuple(round(v, 6) for v in k): t for k, t in bbox_b.items()}
        shared = set(keys_a.keys()) & set(keys_b.keys())
        assert len(shared) > 0, (
            f"No shared tiles between original and shifted grids. "
            f"Grid A has {len(grid_a.tiles)} tiles, grid B has {len(grid_b.tiles)} tiles."
        )

        # Pick a shared tile and fetch it from both grids.
        key = sorted(shared)[0]
        tile_a = keys_a[key]
        tile_b = keys_b[key]
        bounds_a = tile_bbox(grid_a.pixel_grid, tile_a)
        bounds_b = tile_bbox(grid_b.pixel_grid, tile_b)
        for v_a, v_b in zip(bounds_a, bounds_b, strict=True):
            assert abs(v_a - v_b) < 1e-6

        # Fetch the same tile from both grids: should be pixel-identical.
        pixels_a = _fetch_tile_as_numpy(
            client,
            project,
            token,
            expression,
            bounds_a,
            tile_size_pixels,
            crs,
        )
        pixels_b = _fetch_tile_as_numpy(
            client,
            project,
            token,
            expression,
            bounds_b,
            tile_size_pixels,
            crs,
        )

        assert pixels_a.shape == pixels_b.shape, (
            f"Shape mismatch: {pixels_a.shape} vs {pixels_b.shape}"
        )
        np.testing.assert_array_equal(
            pixels_a,
            pixels_b,
            err_msg=(
                f"Pixel mismatch in shared tile with bbox {bounds_a}. "
                f"This means the grid is not snapped to a global origin: "
                f"shifting the region by {shift_pixels}px produced different "
                f"pixel values for the same geographic location."
            ),
        )

    def test_shifted_5px_epsg4326(
        self, gee_project: str, access_token: str, hv_client: httpx.Client
    ) -> None:
        """Shift region by 5 pixels in WGS84: shared tiles must be identical."""
        self._compare_shifted_grids(
            hv_client,
            gee_project,
            access_token,
            _srtm_elevation_expression(),
            crs="EPSG:4326",
            scale_meters=100.0,
            tile_size_pixels=64,
            shift_pixels=5,
        )

    def test_shifted_17px_epsg4326(
        self, gee_project: str, access_token: str, hv_client: httpx.Client
    ) -> None:
        """Shift by 17 pixels (non-power-of-2): still must align."""
        self._compare_shifted_grids(
            hv_client,
            gee_project,
            access_token,
            _srtm_elevation_expression(),
            crs="EPSG:4326",
            scale_meters=100.0,
            tile_size_pixels=64,
            shift_pixels=17,
        )

    def test_shifted_full_tile_epsg4326(
        self, gee_project: str, access_token: str, hv_client: httpx.Client
    ) -> None:
        """Shift by exactly 1 tile width: trivially aligned if grid is snapped."""
        self._compare_shifted_grids(
            hv_client,
            gee_project,
            access_token,
            _srtm_elevation_expression(),
            crs="EPSG:4326",
            scale_meters=100.0,
            tile_size_pixels=64,
            shift_pixels=64,
        )

    def test_shifted_5px_utm(
        self, gee_project: str, access_token: str, hv_client: httpx.Client
    ) -> None:
        """Shift region by 5 pixels in UTM Zone 10N."""
        self._compare_shifted_grids(
            hv_client,
            gee_project,
            access_token,
            _srtm_elevation_expression(),
            crs="EPSG:32610",
            scale_meters=100.0,
            tile_size_pixels=64,
            shift_pixels=5,
        )

    def test_shifted_slope_utm(
        self, gee_project: str, access_token: str, hv_client: httpx.Client
    ) -> None:
        """Shift with slope (derived band) in UTM: hardest combo."""
        self._compare_shifted_grids(
            hv_client,
            gee_project,
            access_token,
            _srtm_slope_expression(),
            crs="EPSG:32610",
            scale_meters=100.0,
            tile_size_pixels=64,
            shift_pixels=10,
        )


# ---------------------------------------------------------------------------
# Full-pipeline two-tier + retry-merge + carryover (requires the pipeline JAR)
# ---------------------------------------------------------------------------


def _find_pipeline_jar():
    from datensee.jar import find_jar

    try:
        return find_jar(None)
    except FileNotFoundError:
        return None


@pytest.mark.integration
class TestTwoTierExportRetryMergeEndToEnd:
    """Run the REAL pipeline (local runner, real EE fetches) through the
    full two-tier + adaptive-retry story:

    1. Two-tier export over a region whose compute-tile snap would NOT
       land on an output-tile boundary (the historical origin-seam bug).
    2. Zero-cost validation suite passes against the real output.
    3. A synthetic journal with a retry-same failure, a split-eligible
       failure, and a terminal failure drives a retry round: the pipeline
       must merge re-fetched tiles (including quadtree split children)
       into the existing COGs, and union the terminal record into the new
       journal (pipeline-side carryover merge).
    4. The nodata value survives to the output COGs' GDAL_NODATA tag.

    Requires the pipeline JAR (``datensee jar build``) and rasterio
    (validation extra); skips with a clear message otherwise.
    """

    REGION = {
        "type": "Polygon",
        "coordinates": [
            [
                [-122.52, 37.32],
                [-122.06, 37.32],
                [-122.06, 37.78],
                [-122.52, 37.78],
                [-122.52, 37.32],
            ]
        ],
    }

    def test_export_validate_retry_merge_carryover(self, gee_project: str, tmp_path) -> None:
        rasterio = pytest.importorskip("rasterio", reason="needs the [validation] extra")
        import numpy as np

        import datensee
        from datensee.pixel.validation import validate_output

        jar = _find_pipeline_jar()
        if jar is None:
            pytest.skip("pipeline JAR not built: run `datensee jar build` first")

        out = tmp_path / "m6-out"
        result = datensee.export(
            ee_expression=datensee.api.demo_expression(),
            region=self.REGION,
            project=gee_project,
            output=str(out),
            scale=90.0,
            crs="EPSG:4326",
            tile_size=256,
            output_tile_size=512,
            nodata=-9999.0,
            runner="local",
            jar=jar,
        )
        assert result.tiles_failed == 0

        # Origin snapped to output-tile boundaries (the seam contract).
        p = result.config.tile_grid.pixel_grid.affine_transform
        assert round(p.translate_x / p.scale_x) % 512 == 0
        assert round(-p.translate_y / p.scale_x) % 512 == 0

        report = validate_output(str(out), result.config)
        assert report.all_passed, "\n".join(
            f"{r.check_id}: {r.status} {r.message}" for r in report.results
        )

        # nodata tag survives the whole chain.
        cogs = sorted(out.glob("tile_*.tif"))
        with rasterio.open(cogs[0]) as ds:
            assert ds.nodata == -9999.0

        # --- Retry round: retry-same + split + terminal carryover ---
        tiles = result.config.tile_grid.tiles
        interior = [t for t in tiles if t.col_px > 0 and t.row_px > 0]
        retry_same_victim = interior[0]
        split_victim = next(t for t in tiles if t is not retry_same_victim)

        before = {f.name: f.read_bytes() for f in cogs}
        journal = out / "_failures.json"
        journal.write_text(
            "\n".join(
                json.dumps(rec)
                for rec in (
                    {**retry_same_victim.model_dump(), "error_kind": "RATE_LIMITED"},
                    {**split_victim.model_dump(), "error_kind": "MEMORY_EXCEEDED"},
                    {**tiles[0].model_dump(), "error_kind": "AUTH_ERROR"},
                )
            )
            + "\n"
        )

        rr = datensee.api.retry(output=str(out), runner="local", jar=jar)
        assert rr.stats.get("retry_same") == 1
        assert rr.stats.get("split") == 1
        assert rr.stats.get("terminal") == 1
        assert rr.next_tiles_count == 5  # 1 same-rect + 4 children
        assert rr.tiles_failed_this_round == 0
        assert rr.carryover_count == 1

        # Pipeline-side carryover union: the journal now holds exactly the
        # terminal record, stamped by the retry planner.
        merged = [json.loads(line) for line in journal.read_text().splitlines() if line.strip()]
        assert len(merged) == 1
        assert merged[0]["error_kind"] == "AUTH_ERROR"
        assert merged[0]["journal_reason"] == "terminal"

        # Merge semantics: deterministic re-fetches leave every COG
        # pixel-identical (the old bug zero-filled everything the round
        # didn't touch), and COGs outside the retried groups are
        # byte-identical.
        touched = {
            f"tile_r{t.out_row:04d}_c{t.out_col:04d}.tif" for t in (retry_same_victim, split_victim)
        }
        for cog_path in cogs:
            if cog_path.name in touched:
                with rasterio.open(cog_path) as ds:
                    after_px = ds.read()
                with rasterio.io.MemoryFile(before[cog_path.name]) as mf, mf.open() as ds:
                    before_px = ds.read()
                assert np.array_equal(
                    np.nan_to_num(before_px, nan=-1.0e30),
                    np.nan_to_num(after_px, nan=-1.0e30),
                ), f"{cog_path.name}: retry merge changed pixels"
                with rasterio.open(cog_path) as ds:
                    assert ds.nodata == -9999.0, "nodata must survive the merge"
            else:
                assert cog_path.read_bytes() == before[cog_path.name], (
                    f"{cog_path.name} modified by retry"
                )
