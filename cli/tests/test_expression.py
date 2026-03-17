"""Tests for EE expression composition (clip wrapping)."""

from __future__ import annotations

import json

from datensee.expression import clip_expression


_SIMPLE_EXPRESSION = json.dumps({"result": "0", "values": {"0": {"constantValue": 42}}})

_POLYGON = {
    "type": "Polygon",
    "coordinates": [
        [[-122.5, 37.75], [-122.25, 37.75], [-122.25, 38.0], [-122.5, 38.0], [-122.5, 37.75]]
    ],
}


def test_clip_wraps_in_image_clip() -> None:
    result = json.loads(clip_expression(_SIMPLE_EXPRESSION, _POLYGON))
    invocation = result["values"]["0"]["functionInvocationValue"]
    assert invocation["functionName"] == "Image.clip"


def test_clip_preserves_original_expression() -> None:
    original = json.loads(_SIMPLE_EXPRESSION)
    result = json.loads(clip_expression(_SIMPLE_EXPRESSION, _POLYGON))
    inner = result["values"]["0"]["functionInvocationValue"]["arguments"]["input"]
    assert inner == original


def test_clip_embeds_geometry() -> None:
    result = json.loads(clip_expression(_SIMPLE_EXPRESSION, _POLYGON))
    geometry = result["values"]["0"]["functionInvocationValue"]["arguments"]["geometry"]
    assert geometry["constantValue"] == _POLYGON


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
    geometry = result["values"]["0"]["functionInvocationValue"]["arguments"]["geometry"]
    assert geometry["constantValue"]["type"] == "MultiPolygon"
