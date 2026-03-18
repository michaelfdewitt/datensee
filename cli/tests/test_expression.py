"""Tests for EE expression composition (clip wrapping)."""

from __future__ import annotations

import json

import pytest

from datensee.expression import clip_expression

_SIMPLE_EXPRESSION = json.dumps({"result": "0", "values": {"0": {"constantValue": 42}}})

_POLYGON = {
    "type": "Polygon",
    "coordinates": [
        [[-122.5, 37.75], [-122.25, 37.75], [-122.25, 38.0], [-122.5, 38.0], [-122.5, 37.75]]
    ],
}


def _get_clip_node(result: dict) -> dict:
    """Return the Image.clip function invocation from a clipped expression."""
    clip_key = result["result"]
    return result["values"][clip_key]["functionInvocationValue"]


def test_clip_wraps_in_image_clip() -> None:
    result = json.loads(clip_expression(_SIMPLE_EXPRESSION, _POLYGON))
    invocation = _get_clip_node(result)
    assert invocation["functionName"] == "Image.clip"


def test_clip_references_original_via_value_reference() -> None:
    result = json.loads(clip_expression(_SIMPLE_EXPRESSION, _POLYGON))
    invocation = _get_clip_node(result)
    assert invocation["arguments"]["input"] == {"valueReference": "0"}


def test_clip_geometry_uses_geometry_constructor() -> None:
    """The geometry should use GeometryConstructors.Polygon."""
    result = json.loads(clip_expression(_SIMPLE_EXPRESSION, _POLYGON))
    invocation = _get_clip_node(result)
    geom_ref = invocation["arguments"]["geometry"]
    assert "valueReference" in geom_ref
    geom_key = geom_ref["valueReference"]
    geom_node = result["values"][geom_key]["functionInvocationValue"]
    assert geom_node["functionName"] == "GeometryConstructors.Polygon"
    assert geom_node["arguments"]["coordinates"]["constantValue"] == _POLYGON["coordinates"]


def test_clip_preserves_original_values() -> None:
    original = json.loads(_SIMPLE_EXPRESSION)
    result = json.loads(clip_expression(_SIMPLE_EXPRESSION, _POLYGON))
    for key, value in original["values"].items():
        assert result["values"][key] == value


def test_clip_output_is_valid_json() -> None:
    result = clip_expression(_SIMPLE_EXPRESSION, _POLYGON)
    parsed = json.loads(result)
    assert "result" in parsed
    assert "values" in parsed


def test_clip_with_multipolygon() -> None:
    multi = {
        "type": "MultiPolygon",
        "coordinates": [_POLYGON["coordinates"], _POLYGON["coordinates"]],
    }
    result = json.loads(clip_expression(_SIMPLE_EXPRESSION, multi))
    invocation = _get_clip_node(result)
    geom_key = invocation["arguments"]["geometry"]["valueReference"]
    geom_node = result["values"][geom_key]["functionInvocationValue"]
    assert geom_node["functionName"] == "GeometryConstructors.MultiPolygon"


def test_clip_rejects_unsupported_geometry_type() -> None:
    point = {"type": "Point", "coordinates": [0, 0]}
    with pytest.raises(ValueError, match="Unsupported geometry type"):
        clip_expression(_SIMPLE_EXPRESSION, point)


def test_clip_key_avoids_collision() -> None:
    expr = json.dumps(
        {
            "result": "0",
            "values": {
                "0": {"constantValue": 42},
                "_clip_0": {"constantValue": 99},
                "_geom_0": {"constantValue": 88},
            },
        }
    )
    result = json.loads(clip_expression(expr, _POLYGON))
    clip_key = result["result"]
    assert clip_key not in ("_clip_0", "_geom_0")
    assert result["values"][clip_key]["functionInvocationValue"]["functionName"] == "Image.clip"
