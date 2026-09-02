"""Tests for datensee.api — public Python API surface."""

from __future__ import annotations

import json

import pytest

from datensee.api import (
    ExportResult,
    demo_expression,
    demo_region,
    tile,
)


class TestTile:
    def test_returns_tile_grid(self) -> None:
        grid = tile(demo_region(), scale=30.0, crs="EPSG:4326", tile_size=512)
        assert grid.tiles is not None
        assert len(grid.tiles) > 0
        assert grid.crs == "EPSG:4326"
        # 30 m at the equator constant ≈ 0.0002695 °/px.
        assert grid.pixel_size > 0
        assert grid.pixel_size < 0.001

    def test_tile_count_varies_with_scale(self) -> None:
        region = demo_region()
        grid_30 = tile(region, scale=30.0)
        grid_100 = tile(region, scale=100.0)
        assert len(grid_30.tiles) >= len(grid_100.tiles)


class TestExportResult:
    def test_model_fields(self) -> None:
        from datensee.config import (
            OutputConfig,
            PipelineConfig,
            RunnerConfig,
        )

        grid = tile(demo_region(), scale=30.0)
        config = PipelineConfig(
            ee_expression=demo_expression(),
            gee_project="test-project",
            tile_grid=grid,
            output=OutputConfig(output_path="/tmp/test"),
            runner=RunnerConfig(mode="local"),
        )
        result = ExportResult(config=config)
        assert result.job_id is None
        assert result.duration_seconds is None
        assert result.output_bytes is None


class TestExportValidation:
    def test_export_raises_on_bad_inputs(self) -> None:
        from datensee.api import export

        with pytest.raises(ValueError, match="not valid JSON"):
            export(
                ee_expression="not json",
                region=demo_region(),
                project="test",
                output="/tmp/out",
                runner="local",
                dry_run=True,
            )

    def test_export_raises_on_missing_temp_location(self) -> None:
        from datensee.api import export

        with pytest.raises(ValueError, match="temp-location"):
            export(
                ee_expression=demo_expression(),
                region=demo_region(),
                project="test",
                output="gs://bucket/out",
                runner="dataflow",
                dry_run=True,
            )

    def test_export_dry_run_succeeds(self) -> None:
        from datensee.api import export

        result = export(
            ee_expression=demo_expression(),
            region=demo_region(),
            project="test",
            output="gs://bucket/out",
            runner="dataflow",
            temp_location="gs://bucket/tmp",
            dry_run=True,
        )
        assert isinstance(result, ExportResult)
        assert result.job_id is None
        assert result.config.runner.mode == "dataflow"


class TestPublicImports:
    def test_top_level_imports(self) -> None:
        from datensee import ExportResult, demo, export, poll, tile

        assert callable(export)
        assert callable(demo)
        assert callable(poll)
        assert callable(tile)
        assert issubclass(ExportResult, object)


# ---------------------------------------------------------------------------
# Input normalization — live ee objects / shapely / GeoJSON
# ---------------------------------------------------------------------------


def _fake_ee(class_name: str, **attrs):
    """Instantiate a duck-typed stand-in for an ``ee`` object.

    The normalizers identify ee objects by ``type(obj).__module__`` — no
    real earthengine-api needed in the test environment.
    """
    cls = type(class_name, (), {"__module__": "ee", **attrs})
    return cls()


_CLOUD_EXPR = '{"result":"0","values":{"0":{"constantValue":1}}}'


class TestNormalizeExpression:
    def test_string_passes_through(self) -> None:
        from datensee.api import _normalize_expression

        assert _normalize_expression(_CLOUD_EXPR) == _CLOUD_EXPR

    def test_ee_image_is_serialized_with_callers_client(self) -> None:
        from datensee.api import _normalize_expression

        image = _fake_ee("Image", serialize=lambda self: _CLOUD_EXPR)
        assert _normalize_expression(image) == _CLOUD_EXPR

    def test_ee_image_collection_is_rejected_with_reduce_hint(self) -> None:
        from datensee.api import _normalize_expression

        collection = _fake_ee("ImageCollection", serialize=lambda self: _CLOUD_EXPR)
        with pytest.raises(ValueError, match=r"median\(\)"):
            _normalize_expression(collection)

    def test_legacy_serializer_format_is_rejected(self) -> None:
        from datensee.api import _normalize_expression

        legacy = _fake_ee("Image", serialize=lambda self: '{"type":"Invocation"}')
        with pytest.raises(ValueError, match="cloud-API"):
            _normalize_expression(legacy)

    def test_non_json_serialize_output_is_rejected(self) -> None:
        from datensee.api import _normalize_expression

        broken = _fake_ee("Image", serialize=lambda self: "not-json")
        with pytest.raises(ValueError, match="legacy serialization"):
            _normalize_expression(broken)

    def test_unrelated_object_raises_type_error(self) -> None:
        from datensee.api import _normalize_expression

        with pytest.raises(TypeError, match="ee.Image"):
            _normalize_expression(42)

    def test_collection_result_expression_string_is_rejected(self) -> None:
        from datensee.api import _reject_collection_expression

        expr = json.dumps(
            {
                "result": "0",
                "values": {
                    "0": {
                        "functionInvocationValue": {
                            "functionName": "Collection.filter",
                            "arguments": {},
                        }
                    }
                },
            }
        )
        with pytest.raises(ValueError, match="ImageCollection"):
            _reject_collection_expression(expr)


class TestNormalizeRegion:
    _GEOJSON = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}

    def test_dict_passes_through(self) -> None:
        from datensee.api import _normalize_region

        assert _normalize_region(self._GEOJSON) == self._GEOJSON

    def test_shapely_geometry_via_geo_interface(self) -> None:
        from shapely.geometry import Polygon

        from datensee.api import _normalize_region

        result = _normalize_region(Polygon([(0, 0), (1, 0), (1, 1)]))
        assert result["type"] == "Polygon"

    def test_ee_geometry_uses_client_side_to_geojson(self) -> None:
        from datensee.api import _normalize_region

        geom = _fake_ee("Geometry", toGeoJSON=lambda self: dict(self._GEOJSON_))
        geom._GEOJSON_ = self._GEOJSON
        assert _normalize_region(geom) == self._GEOJSON

    def test_server_computed_geometry_falls_back_to_get_info(self) -> None:
        from datensee.api import _normalize_region

        def _raise(self):
            raise RuntimeError("server-side geometry")

        expected = self._GEOJSON
        geom = _fake_ee("Geometry", toGeoJSON=_raise, getInfo=lambda self: dict(expected))
        assert _normalize_region(geom) == expected

    def test_ee_feature_resolves_via_geometry(self) -> None:
        from datensee.api import _normalize_region

        expected = self._GEOJSON
        geom = _fake_ee("Geometry", toGeoJSON=lambda self: dict(expected))
        feature = _fake_ee("Feature", geometry=lambda self: geom)
        assert _normalize_region(feature) == expected

    def test_unrelated_object_raises_type_error(self) -> None:
        from datensee.api import _normalize_region

        with pytest.raises(TypeError, match="GeoJSON"):
            _normalize_region("POLYGON((0 0,1 0,1 1,0 0))")


class TestValidateInputs:
    """Compact input-validation coverage (successor to the old 23-test file)."""

    _EXPR = _CLOUD_EXPR
    _POLY = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}

    def _errors(self, **overrides) -> list[str]:
        from datensee.api import validate_inputs

        kwargs = dict(
            ee_expression=self._EXPR,
            geojson_geometry=self._POLY,
            crs="EPSG:4326",
            output="./out",
            runner="local",
        )
        kwargs.update(overrides)
        return validate_inputs(**kwargs)

    def test_valid_inputs_produce_no_errors(self) -> None:
        assert self._errors() == []

    def test_malformed_expression_json(self) -> None:
        assert any("not valid JSON" in e for e in self._errors(ee_expression="{nope"))

    def test_feature_collection_region_rejected_with_hint(self) -> None:
        errors = self._errors(geojson_geometry={"type": "FeatureCollection", "features": []})
        assert any("FeatureCollection" in e for e in errors)

    def test_point_region_rejected(self) -> None:
        errors = self._errors(geojson_geometry={"type": "Point", "coordinates": [0, 0]})
        assert any("Polygon" in e for e in errors)

    def test_unknown_crs_rejected(self) -> None:
        assert any("not recognized" in e for e in self._errors(crs="EPSG:99999999"))

    def test_dataflow_requires_gcs_output(self) -> None:
        errors = self._errors(runner="dataflow", output="./local-dir")
        assert any("gs://" in e for e in errors)
