"""Tests for CLI input validation — realistic bad-input scenarios.

These test the validate_inputs function with inputs that real users are
likely to provide incorrectly: wrong GeoJSON types, malformed expressions,
unsupported CRS codes, Feature wrappers, etc.
"""

from __future__ import annotations

import json

from datensee.api import validate_inputs

# ---------------------------------------------------------------------------
# Realistic EE expression fixtures
# ---------------------------------------------------------------------------

_VALID_NDVI_EXPRESSION = json.dumps(
    {
        "result": "0",
        "values": {
            "0": {
                "functionInvocationValue": {
                    "functionName": "Image.normalizedDifference",
                    "arguments": {
                        "bandNames": {"constantValue": ["SR_B5", "SR_B4"]},
                        "input": {
                            "functionInvocationValue": {
                                "functionName": "ImageCollection.load",
                                "arguments": {"id": {"constantValue": "LANDSAT/LC09/C02/T1_L2"}},
                            }
                        },
                    },
                }
            }
        },
    }
)


# ---------------------------------------------------------------------------
# Region fixtures
# ---------------------------------------------------------------------------

_POLYGON_SF = {
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

_MULTIPOLYGON_HAWAII = {
    "type": "MultiPolygon",
    "coordinates": [
        [
            [
                [-155.5, 19.4],
                [-154.8, 19.4],
                [-154.8, 20.0],
                [-155.5, 20.0],
                [-155.5, 19.4],
            ]
        ],
        [
            [
                [-156.7, 20.8],
                [-156.1, 20.8],
                [-156.1, 21.2],
                [-156.7, 21.2],
                [-156.7, 20.8],
            ]
        ],
    ],
}

_FEATURE_WRAPPING_POLYGON = {
    "type": "Feature",
    "properties": {"name": "SF Bay Area"},
    "geometry": _POLYGON_SF,
}

_POINT_GEOMETRY = {
    "type": "Point",
    "coordinates": [-122.4194, 37.7749],
}

_LINESTRING_GEOMETRY = {
    "type": "LineString",
    "coordinates": [[-122.5, 37.75], [-122.25, 38.0]],
}

_FEATURE_COLLECTION = {
    "type": "FeatureCollection",
    "features": [
        {"type": "Feature", "geometry": _POLYGON_SF, "properties": {}},
    ],
}

_FEATURE_WRAPPING_POINT = {
    "type": "Feature",
    "properties": {},
    "geometry": _POINT_GEOMETRY,
}


# ---------------------------------------------------------------------------
# Happy paths — no errors expected
# ---------------------------------------------------------------------------


class TestValidInputs:
    def test_polygon_epsg4326_local(self) -> None:
        errors = validate_inputs(
            _VALID_NDVI_EXPRESSION, _POLYGON_SF, "EPSG:4326", "/tmp/out", "local"
        )
        assert errors == []

    def test_multipolygon_accepted(self) -> None:
        errors = validate_inputs(
            _VALID_NDVI_EXPRESSION,
            _MULTIPOLYGON_HAWAII,
            "EPSG:4326",
            "/tmp/out",
            "local",
        )
        assert errors == []

    def test_feature_wrapping_polygon_accepted(self) -> None:
        """GeoJSON Feature with a Polygon geometry should pass validation."""
        errors = validate_inputs(
            _VALID_NDVI_EXPRESSION,
            _FEATURE_WRAPPING_POLYGON,
            "EPSG:4326",
            "/tmp/out",
            "local",
        )
        assert errors == []

    def test_utm_crs_accepted(self) -> None:
        errors = validate_inputs(
            _VALID_NDVI_EXPRESSION,
            _POLYGON_SF,
            "EPSG:32610",
            "/tmp/out",
            "local",
        )
        assert errors == []

    def test_gcs_output_local_runner(self) -> None:
        """GCS paths are fine even in local mode (Beam direct runner can write GCS)."""
        errors = validate_inputs(
            _VALID_NDVI_EXPRESSION,
            _POLYGON_SF,
            "EPSG:4326",
            "gs://my-bucket/output",
            "local",
        )
        assert errors == []

    def test_gcs_output_dataflow_runner(self) -> None:
        errors = validate_inputs(
            _VALID_NDVI_EXPRESSION,
            _POLYGON_SF,
            "EPSG:4326",
            "gs://my-bucket/output",
            "dataflow",
        )
        assert errors == []

    def test_minimal_valid_expression(self) -> None:
        """Even a trivial JSON object should pass the expression check."""
        errors = validate_inputs("{}", _POLYGON_SF, "EPSG:4326", "/tmp/out", "local")
        assert errors == []


# ---------------------------------------------------------------------------
# Bad expression inputs
# ---------------------------------------------------------------------------


class TestBadExpressions:
    def test_plain_text_rejected(self) -> None:
        errors = validate_inputs("not json at all", _POLYGON_SF, "EPSG:4326", "/tmp/out", "local")
        assert len(errors) == 1
        assert "not valid JSON" in errors[0]

    def test_truncated_json_rejected(self) -> None:
        """Simulates a truncated file copy."""
        errors = validate_inputs(
            '{"result": "0", "values": {',
            _POLYGON_SF,
            "EPSG:4326",
            "/tmp/out",
            "local",
        )
        assert len(errors) == 1
        assert "not valid JSON" in errors[0]

    def test_yaml_rejected(self) -> None:
        """Users might accidentally provide YAML."""
        errors = validate_inputs(
            "result: 0\nvalues:\n  0: test",
            _POLYGON_SF,
            "EPSG:4326",
            "/tmp/out",
            "local",
        )
        assert len(errors) == 1
        assert "not valid JSON" in errors[0]

    def test_empty_string_rejected(self) -> None:
        errors = validate_inputs("", _POLYGON_SF, "EPSG:4326", "/tmp/out", "local")
        assert len(errors) == 1
        assert "not valid JSON" in errors[0]


# ---------------------------------------------------------------------------
# Bad region inputs
# ---------------------------------------------------------------------------


class TestBadRegions:
    def test_point_geometry_rejected(self) -> None:
        errors = validate_inputs(
            _VALID_NDVI_EXPRESSION,
            _POINT_GEOMETRY,
            "EPSG:4326",
            "/tmp/out",
            "local",
        )
        assert len(errors) == 1
        assert "Point" in errors[0]

    def test_linestring_rejected(self) -> None:
        errors = validate_inputs(
            _VALID_NDVI_EXPRESSION,
            _LINESTRING_GEOMETRY,
            "EPSG:4326",
            "/tmp/out",
            "local",
        )
        assert len(errors) == 1
        assert "LineString" in errors[0]

    def test_feature_collection_rejected(self) -> None:
        errors = validate_inputs(
            _VALID_NDVI_EXPRESSION,
            _FEATURE_COLLECTION,
            "EPSG:4326",
            "/tmp/out",
            "local",
        )
        assert len(errors) == 1
        assert "FeatureCollection" in errors[0]

    def test_feature_wrapping_point_rejected(self) -> None:
        """A Feature wrapper doesn't help if the inner geometry is a Point."""
        errors = validate_inputs(
            _VALID_NDVI_EXPRESSION,
            _FEATURE_WRAPPING_POINT,
            "EPSG:4326",
            "/tmp/out",
            "local",
        )
        assert len(errors) == 1
        assert "Point" in errors[0]

    def test_missing_type_field(self) -> None:
        errors = validate_inputs(
            _VALID_NDVI_EXPRESSION,
            {"coordinates": [[0, 0], [1, 1]]},
            "EPSG:4326",
            "/tmp/out",
            "local",
        )
        assert len(errors) == 1
        assert "None" in errors[0]


# ---------------------------------------------------------------------------
# Bad CRS inputs
# ---------------------------------------------------------------------------


class TestBadCrs:
    def test_garbage_crs_rejected(self) -> None:
        errors = validate_inputs(
            _VALID_NDVI_EXPRESSION,
            _POLYGON_SF,
            "not-a-crs",
            "/tmp/out",
            "local",
        )
        assert len(errors) == 1
        assert "not recognized" in errors[0]

    def test_epsg_typo_rejected(self) -> None:
        """EPSG:99999 doesn't exist."""
        errors = validate_inputs(
            _VALID_NDVI_EXPRESSION,
            _POLYGON_SF,
            "EPSG:99999",
            "/tmp/out",
            "local",
        )
        assert len(errors) == 1
        assert "not recognized" in errors[0]

    def test_valid_uncommon_crs_accepted(self) -> None:
        """EPSG:3857 (Web Mercator) is valid even if unusual for EE exports."""
        errors = validate_inputs(
            _VALID_NDVI_EXPRESSION,
            _POLYGON_SF,
            "EPSG:3857",
            "/tmp/out",
            "local",
        )
        assert errors == []


# ---------------------------------------------------------------------------
# Bad output paths
# ---------------------------------------------------------------------------


class TestBadOutputPaths:
    def test_dataflow_requires_gcs(self) -> None:
        errors = validate_inputs(
            _VALID_NDVI_EXPRESSION,
            _POLYGON_SF,
            "EPSG:4326",
            "/local/path",
            "dataflow",
        )
        assert len(errors) == 1
        assert "gs://" in errors[0]

    def test_dataflow_accepts_gcs(self) -> None:
        errors = validate_inputs(
            _VALID_NDVI_EXPRESSION,
            _POLYGON_SF,
            "EPSG:4326",
            "gs://my-bucket/output",
            "dataflow",
        )
        assert errors == []


# ---------------------------------------------------------------------------
# Multiple simultaneous errors
# ---------------------------------------------------------------------------


class TestMultipleErrors:
    def test_bad_expression_and_bad_region(self) -> None:
        """All validation errors are collected, not short-circuited."""
        errors = validate_inputs(
            "not json",
            _POINT_GEOMETRY,
            "EPSG:4326",
            "/tmp/out",
            "local",
        )
        assert len(errors) == 2
        assert any("JSON" in e for e in errors)
        assert any("Point" in e for e in errors)

    def test_bad_everything(self) -> None:
        errors = validate_inputs(
            "not json",
            _POINT_GEOMETRY,
            "BOGUS",
            "/local/path",
            "dataflow",
        )
        assert len(errors) >= 3
