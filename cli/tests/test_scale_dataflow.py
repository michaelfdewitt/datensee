"""Large-scale Dataflow integration test: Sentinel-2 NDVI over Switzerland.

This test submits a real Dataflow job that fetches ~3,000 tiles at 10m
resolution, producing ~2-3 GB of output GeoTIFFs. It exercises the full
end-to-end pipeline: Python CLI → config → Java Beam pipeline → Dataflow
workers → EE HV API → GCS output.

Run with:
    cd cli && uv run pytest tests/test_scale_dataflow.py \
        --scale \
        --gee-project=datensee-testing \
        --gcs-bucket=datensee-testing \
        -v -s

Dataflow region: us-east1 (co-located with EE backends for minimum latency).

Expected cost per run:
    - EECUs: ~3,000-9,000 EECU-seconds (~1-2.5 EECU-hours)
    - Dataflow: ~$0.20-0.50 (n2-standard-4 × 50 workers, ~5-15 min)
    - GCS storage: ~$0.05/month (cleaned up by default, --keep-output to retain)
    - Total: ~$1-3 per run depending on EE load

Requirements:
    - Application Default Credentials configured
    - Project has Earth Engine API + Dataflow API + Cloud Storage enabled
    - Pipeline JAR built: cd pipelines && ./gradlew shadowJar
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from datensee.config import (
    DataflowRunnerConfig,
    OutputConfig,
    PipelineConfig,
    RunnerConfig,
)
from datensee.display import _format_bytes
from datensee.jar import find_jar
from datensee.pixel.tiling import decompose_region
from datensee.submit import submit_job

# ---------------------------------------------------------------------------
# Sentinel-2 NDVI expression
# ---------------------------------------------------------------------------


def _sentinel2_ndvi_expression() -> str:
    """Sentinel-2 summer 2023 median NDVI (single band, float32).

    Equivalent to:
        ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED')
          .filterDate('2023-06-01', '2023-09-01')
          .median()
          .normalizedDifference(['B8', 'B4'])
    """
    return json.dumps(
        {
            "result": "0",
            "values": {
                "0": {
                    "functionInvocationValue": {
                        "functionName": "Image.normalizedDifference",
                        "arguments": {
                            "bandNames": {"constantValue": ["B8", "B4"]},
                            "input": {
                                "functionInvocationValue": {
                                    "functionName": "reduce.median",
                                    "arguments": {
                                        "collection": {
                                            "functionInvocationValue": {
                                                "functionName": "Collection.filter",
                                                "arguments": {
                                                    "collection": {
                                                        "functionInvocationValue": {
                                                            "functionName": (
                                                                "ImageCollection.load"
                                                            ),
                                                            "arguments": {
                                                                "id": {
                                                                    "constantValue": (
                                                                        "COPERNICUS"
                                                                        "/S2_SR_HARMONIZED"
                                                                    )
                                                                }
                                                            },
                                                        }
                                                    },
                                                    "filter": {
                                                        "functionInvocationValue": {
                                                            "functionName": (
                                                                "Filter.dateRangeContains"
                                                            ),
                                                            "arguments": {
                                                                "leftValue": {
                                                                    "functionInvocationValue": {
                                                                        "functionName": "DateRange",
                                                                        "arguments": {
                                                                            "start": {
                                                                                "constantValue": (
                                                                                    "2023-06-01"
                                                                                )
                                                                            },
                                                                            "end": {
                                                                                "constantValue": (
                                                                                    "2023-09-01"
                                                                                )
                                                                            },
                                                                        },
                                                                    }
                                                                },
                                                                "rightField": {
                                                                    "constantValue": (
                                                                        "system:time_start"
                                                                    )
                                                                },
                                                            },
                                                        }
                                                    },
                                                },
                                            }
                                        }
                                    },
                                }
                            },
                        },
                    }
                }
            },
        }
    )


# ---------------------------------------------------------------------------
# Region: Greater Zürich → Switzerland
# ---------------------------------------------------------------------------

# Covers most of Switzerland plus border regions.
# ~330 km × ~220 km → ~3,000 tiles at 10m / 512px.
_SWITZERLAND = {
    "type": "Polygon",
    "coordinates": [
        [
            [6.0, 46.0],
            [10.5, 46.0],
            [10.5, 48.0],
            [6.0, 48.0],
            [6.0, 46.0],
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
def gcs_bucket(request: pytest.FixtureRequest) -> str:
    bucket = request.config.getoption("--gcs-bucket")
    if not bucket:
        pytest.skip("--gcs-bucket not provided")
    return bucket


@pytest.fixture(scope="module")
def keep_output(request: pytest.FixtureRequest) -> bool:
    return request.config.getoption("--keep-output")


@pytest.fixture(scope="module")
def jar_path() -> Path:
    return find_jar()


@pytest.fixture(scope="module")
def output_prefix(gcs_bucket: str) -> str:
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"gs://{gcs_bucket}/scale-test/zurich-ndvi-{ts}"


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.scale
class TestZurichNdviDataflow:
    """End-to-end Dataflow export: Sentinel-2 NDVI over Switzerland at 10m."""

    def test_full_pipeline(
        self,
        gee_project: str,
        gcs_bucket: str,
        keep_output: bool,
        jar_path: Path,
        output_prefix: str,
    ) -> None:
        """Submit Dataflow job, wait for completion, verify GCS output.

        This test:
        1. Tiles Switzerland at 10m in EPSG:32632 (~3,000 tiles)
        2. Prints a cost estimate
        3. Submits to Dataflow with 50 workers
        4. Waits for the pipeline subprocess to complete
        5. Verifies tile count and total size on GCS
        6. Cleans up (unless --keep-output)
        """
        # -- Tile the region --
        grid = decompose_region(
            _SWITZERLAND,
            scale_meters=10.0,
            crs="EPSG:32632",
            tile_size_pixels=512,
        )
        tile_count = len(grid.tiles)
        assert tile_count > 2000, f"Expected >2000 tiles, got {tile_count}"
        print(f"\nTile count: {tile_count}")

        # -- Build config --
        # No EE-side clip: decompose-time region intersection keeps
        # out-of-region tiles out of the workload entirely.
        expression = _sentinel2_ndvi_expression()

        temp_location = f"gs://{gcs_bucket}/dataflow-temp"

        config = PipelineConfig(
            ee_expression=expression,
            gee_project=gee_project,
            tile_grid=grid,
            output=OutputConfig(
                output_path=output_prefix,
                band_count=1,
                data_type="float32",
            ),
            runner=RunnerConfig(
                mode="dataflow",
                dataflow=DataflowRunnerConfig(
                    project=gee_project,
                    region="us-east1",
                    temp_location=temp_location,
                    staging_location=temp_location + "/staging",
                    machine_type="n2-standard-4",
                    max_workers=50,
                ),
            ),
        )

        # -- Config summary --
        print(f"Tiles: {config.tile_count}")
        print(f"Raw output size: {_format_bytes(config.raw_output_bytes)}")
        print(f"Output: {output_prefix}")

        # -- Submit and wait --
        t0 = time.monotonic()
        submit_job(config, jar_path=jar_path)
        duration = time.monotonic() - t0
        print(f"Pipeline completed in {duration:.0f}s ({duration / 60:.1f} min)")

        # -- Verify GCS output --
        from google.cloud import storage

        client = storage.Client(project=gee_project)
        prefix = output_prefix.replace(f"gs://{gcs_bucket}/", "")
        blobs = list(client.list_blobs(gcs_bucket, prefix=prefix))

        tile_blobs = [b for b in blobs if b.name.endswith(".tif")]
        total_bytes = sum(b.size for b in tile_blobs)
        total_gb = total_bytes / (1024**3)

        print(f"GCS tiles: {len(tile_blobs)}")
        print(f"Total size: {total_gb:.2f} GB")

        # Allow some tile failures (partial failure tolerance from M3),
        # but at least 95% of tiles should succeed.
        min_tiles = int(tile_count * 0.95)
        assert len(tile_blobs) >= min_tiles, (
            f"Expected ≥{min_tiles} tiles on GCS, got {len(tile_blobs)}. "
            f"Too many tile failures ({tile_count - len(tile_blobs)} failed)."
        )

        # Output should be in the GB range for a valid multi-GB export.
        assert total_gb > 0.5, (
            f"Expected >0.5 GB total output, got {total_gb:.2f} GB. "
            "Tiles may be empty or corrupted."
        )

        # Spot-check: each tile should be non-trivially sized (>10 KB).
        # Empty/error tiles would be tiny.
        small_tiles = [b for b in tile_blobs if b.size < 10_000]
        assert len(small_tiles) < tile_count * 0.05, (
            f"{len(small_tiles)} tiles are suspiciously small (<10 KB). "
            "Possible fetch failures or empty tiles."
        )

        print(f"PASS: {len(tile_blobs)} tiles, {total_gb:.2f} GB")

        # -- Cleanup --
        if not keep_output:
            from google.api_core.exceptions import NotFound

            print(f"Cleaning up {len(blobs)} objects from {output_prefix}")
            for blob in blobs:
                try:
                    blob.delete()
                except NotFound:
                    pass
            # Also clean up dataflow temp
            temp_prefix = "dataflow-temp"
            temp_blobs = list(client.list_blobs(gcs_bucket, prefix=temp_prefix))
            for blob in temp_blobs:
                try:
                    blob.delete()
                except NotFound:
                    pass
            print(f"Cleaned up {len(temp_blobs)} temp objects")
        else:
            print(f"Output retained at {output_prefix}")
