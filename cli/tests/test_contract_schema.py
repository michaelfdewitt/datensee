"""Pin the JSON contract to the Pydantic models.

``contract/pipeline-config.schema.json`` is the wire-format contract the
Java pipeline validates against; the Pydantic models in
``datensee.config`` / ``datensee.pixel.config`` are what actually gets
serialized. These tests build representative configs in Python, dump
them exactly the way ``submit.py`` does
(``model_dump_json(indent=2, exclude_none=True)``), and validate the
result against the schema; any model change that isn't mirrored in
the schema (or vice versa) fails CI instead of surfacing as a Java-side
parse error.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from datensee.config import (
    AffineTransform,
    DataflowRunnerConfig,
    GridDimensions,
    OutputConfig,
    PipelineConfig,
    PixelGrid,
    PixelPayload,
    RunnerConfig,
    TileCoordinate,
    TileGrid,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCHEMA_PATH = _REPO_ROOT / "contract" / "pipeline-config.schema.json"
_EXAMPLE_PATH = _REPO_ROOT / "contract" / "examples" / "ndvi-california.json"

_EE_EXPRESSION = json.dumps({"result": "0", "values": {"0": {"constantValue": 1}}})


@pytest.fixture(scope="module")
def schema() -> dict:
    return json.loads(_SCHEMA_PATH.read_text())


def _pixel_grid(width: int = 1024, height: int = 1024) -> PixelGrid:
    return PixelGrid(
        crs_code="EPSG:4326",
        affine_transform=AffineTransform(
            scale_x=0.000269458,
            translate_x=-122.5,
            scale_y=-0.000269458,
            translate_y=38.5,
        ),
        dimensions=GridDimensions(width=width, height=height),
    )


def _inline_tiles() -> list[TileCoordinate]:
    return [
        TileCoordinate(
            col_px=col * 512,
            row_px=row * 512,
            width_px=512,
            height_px=512,
            row=row,
            col=col,
            out_row=0,
            out_col=0,
        )
        for row in (0, 1)
        for col in (0, 1)
    ]


def _serialized(config: PipelineConfig) -> dict:
    """Serialize exactly the way ``submit.py`` hands the config to the JVM."""
    return json.loads(config.model_dump_json(indent=2, exclude_none=True))


def test_minimal_local_pixel_config_matches_schema(schema: dict) -> None:
    config = PipelineConfig(
        ee_expression=_EE_EXPRESSION,
        gee_project="my-gee-project",
        pixel=PixelPayload(
            tile_grid=TileGrid(pixel_grid=_pixel_grid(), tiles=_inline_tiles()),
            output=OutputConfig(output_path="./datensee-output"),
        ),
    )
    jsonschema.validate(_serialized(config), schema)


def test_m6_dataflow_config_with_all_runner_fields_matches_schema(schema: dict) -> None:
    config = PipelineConfig(
        ee_expression=_EE_EXPRESSION,
        gee_project="my-gee-project",
        snapshot_time=1_715_000_000_000_000,
        runner=RunnerConfig(
            mode="dataflow",
            dataflow=DataflowRunnerConfig(
                project="my-dataflow-project",
                region="us-central1",
                temp_location="gs://my-bucket/tmp",
                staging_location="gs://my-bucket/tmp/staging",
                machine_type="n2-standard-8",
                num_workers=8,
                max_workers=200,
                autoscaling_algorithm="THROUGHPUT_BASED",
                number_of_worker_harness_threads=16,
                service_account_email="worker@my-dataflow-project.iam.gserviceaccount.com",
                network="projects/my-dataflow-project/global/networks/default",
                subnetwork="regions/us-central1/subnetworks/default",
                labels={"caller": "test", "integration": "contract"},
            ),
        ),
        pixel=PixelPayload(
            tile_grid=TileGrid(pixel_grid=_pixel_grid(2048, 2048), tiles=_inline_tiles()),
            output=OutputConfig(
                output_path="gs://my-bucket/exports/run-1/",
                band_count=3,
                data_type="uint16",
                output_tile_size_pixels=1024,
                compression="deflate",
            ),
        ),
    )
    jsonschema.validate(_serialized(config), schema)


def test_retry_shaped_config_matches_schema(schema: dict) -> None:
    config = PipelineConfig(
        ee_expression=_EE_EXPRESSION,
        gee_project="my-gee-project",
        snapshot_time=1_715_000_000_000_000,
        carryover_file="gs://my-bucket/exports/run-1/_carryover.json",
        pixel=PixelPayload(
            tile_grid=TileGrid(
                pixel_grid=_pixel_grid(),
                tiles_file="gs://my-bucket/exports/run-1/_retry_tiles.json",
            ),
            output=OutputConfig(
                output_path="gs://my-bucket/exports/run-1/",
                output_tile_size_pixels=1024,
                merge_existing_output=True,
                nodata=-9999.0,
            ),
        ),
    )
    jsonschema.validate(_serialized(config), schema)


def test_bundled_example_matches_schema_and_models(schema: dict) -> None:
    """The shipped example must satisfy both the schema and the models."""
    example = json.loads(_EXAMPLE_PATH.read_text())
    jsonschema.validate(example, schema)
    PipelineConfig.model_validate(example)
