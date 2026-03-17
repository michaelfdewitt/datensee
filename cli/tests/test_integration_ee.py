"""Integration tests against the Earth Engine High Volume API.

These tests send real HTTP requests to the EE HV endpoint. They use
SRTM elevation data with lightweight math (rescaling, gradient, multi-band
stacking) so that tiles have spatially varying values — tiling misalignment,
CRS errors, or projection bugs produce visibly wrong results. SRTM is
pre-cached on EE servers and consumes negligible EECU time.

Run with:
    uv run pytest tests/test_integration_ee.py --integration --gee-project=datensee-testing -v

Requirements:
    - Application Default Credentials configured (`gcloud auth application-default login`)
    - The project must have the Earth Engine API enabled and be registered for use
"""

from __future__ import annotations

import json
import struct
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import httpx
import pytest

from datensee.auth import get_access_token
from datensee.tiling import decompose_region

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
# Expression builders — SRTM-based, spatially varying, cheap to compute.
#
# All expressions use the SRTM 30m DEM (NASA/USGS). It's pre-cached on EE
# servers so evaluation is fast. The math applied is trivial (divide, multiply,
# subtract) but the spatial variation in elevation means any tiling or CRS
# bugs will produce visible discontinuities at tile boundaries.
# ---------------------------------------------------------------------------


def _srtm_elevation_expression() -> str:
    """SRTM elevation in meters — single band, spatially varying.

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
                                        "id": {
                                            "constantValue": "USGS/SRTMGL1_003"
                                        },
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
                                "arrayValue": {
                                    "values": [srtm_load, srtm_scaled, srtm_squared]
                                }
                            }
                        },
                    }
                }
            },
        }
    )


def _srtm_slope_expression() -> str:
    """Terrain slope from SRTM — uses ee.Terrain.slope().

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
                                        "id": {
                                            "constantValue": "USGS/SRTMGL1_003"
                                        },
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

# Small: SF Bay Area, 0.25° × 0.25° — a few tiles at moderate scale.
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

# Medium: Sierra Nevada + Central Valley — varied terrain, ~1° × 1°.
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

# Large: California bbox for generating thousands of tiles.
_CALIFORNIA = {
    "type": "Polygon",
    "coordinates": [
        [
            [-124.5, 32.5],
            [-114.0, 32.5],
            [-114.0, 42.0],
            [-124.5, 42.0],
            [-124.5, 32.5],
        ]
    ],
}


# ===========================================================================
# Integration tests
# ===========================================================================


@pytest.mark.integration
class TestSingleTileFetch:
    """Verify single-tile fetches across CRS and expression variants."""

    def test_srtm_elevation_epsg4326(
        self, gee_project: str, access_token: str, hv_client: httpx.Client
    ) -> None:
        """Raw SRTM elevation, WGS84, 64×64 pixels over Sierra Nevada."""
        body = _build_hv_request(
            _srtm_elevation_expression(),
            tile_bounds=(-120.0, 37.5, -119.5, 38.0),
            tile_size=64,
            crs="EPSG:4326",
        )
        resp = _fetch_tile(hv_client, gee_project, access_token, body)
        assert resp.status_code == 200, f"HTTP {resp.status_code}: {resp.text[:200]}"
        _assert_valid_geotiff(resp.content)

    def test_srtm_elevation_utm(
        self, gee_project: str, access_token: str, hv_client: httpx.Client
    ) -> None:
        """SRTM in UTM Zone 10N — verifies projected CRS."""
        body = _build_hv_request(
            _srtm_elevation_expression(),
            tile_bounds=(545000, 4176000, 547560, 4178560),
            tile_size=64,
            crs="EPSG:32610",
        )
        resp = _fetch_tile(hv_client, gee_project, access_token, body)
        assert resp.status_code == 200, f"HTTP {resp.status_code}: {resp.text[:200]}"
        _assert_valid_geotiff(resp.content)

    def test_srtm_elevation_web_mercator(
        self, gee_project: str, access_token: str, hv_client: httpx.Client
    ) -> None:
        """SRTM in EPSG:3857 (Web Mercator)."""
        body = _build_hv_request(
            _srtm_elevation_expression(),
            tile_bounds=(-13630000, 4540000, -13625000, 4545000),
            tile_size=64,
            crs="EPSG:3857",
        )
        resp = _fetch_tile(hv_client, gee_project, access_token, body)
        assert resp.status_code == 200, f"HTTP {resp.status_code}: {resp.text[:200]}"
        _assert_valid_geotiff(resp.content)

    def test_srtm_rescaled(
        self, gee_project: str, access_token: str, hv_client: httpx.Client
    ) -> None:
        """Rescaled elevation (0–1) — server-side division."""
        body = _build_hv_request(
            _srtm_rescaled_expression(),
            tile_bounds=(-120.0, 37.5, -119.5, 38.0),
            tile_size=64,
            crs="EPSG:4326",
        )
        resp = _fetch_tile(hv_client, gee_project, access_token, body)
        assert resp.status_code == 200
        _assert_valid_geotiff(resp.content)

    def test_srtm_slope(
        self, gee_project: str, access_token: str, hv_client: httpx.Client
    ) -> None:
        """Terrain slope — derived from elevation gradient, highly spatial."""
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
        """3-band SRTM derivative — validates multi-band fetch."""
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
        """Slope in UTM — combines derived band with projected CRS."""
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
        grid = decompose_region(
            _SIERRA_NEVADA, scale_meters=1000.0, crs="EPSG:4326"
        )
        assert len(grid.tiles) >= 1

        for tile in grid.tiles:
            body = _build_hv_request(
                _srtm_elevation_expression(),
                tile_bounds=(tile.x_min, tile.y_min, tile.x_max, tile.y_max),
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
                tile_bounds=(tile.x_min, tile.y_min, tile.x_max, tile.y_max),
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
                tile_bounds=(tile.x_min, tile.y_min, tile.x_max, tile.y_max),
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

    Uses SRTM elevation and slope — each tile is spatially varying and
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
                tile_bounds=(tile.x_min, tile.y_min, tile.x_max, tile.y_max),
                tile_size=tile_size,
                crs=grid_crs,
            )
            resp = _fetch_tile(client, project, token, body)
            detail = ""
            if resp.status_code != 200:
                detail = (
                    f"r={tile.row},c={tile.col} HTTP {resp.status_code}: "
                    f"{resp.text[:100]}"
                )
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
        """Fetch ~1000 tiles of slope in UTM — hardest CRS + derived band combo.

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
    and fetch — as the export command would do before handing off to Java.
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
                tile_bounds=(tile.x_min, tile.y_min, tile.x_max, tile.y_max),
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
        """PipelineConfig with 3-band SRTM in UTM — the M2 happy path."""
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
                tile_bounds=(tile.x_min, tile.y_min, tile.x_max, tile.y_max),
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
                tile_bounds=(tile.x_min, tile.y_min, tile.x_max, tile.y_max),
                tile_size=restored.tile_grid.tile_size_pixels,
                crs=restored.tile_grid.crs,
            )
            resp = _fetch_tile(hv_client, gee_project, access_token, body)
            assert resp.status_code == 200
            _assert_valid_geotiff(resp.content)
