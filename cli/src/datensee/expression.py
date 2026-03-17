"""EE expression manipulation.

We treat user expressions as opaque — we never interpret the computation
graph. But we do compose with it: wrapping in Image.clip(region) to mask
pixels outside the export region on edge tiles.
"""

from __future__ import annotations

import json
from typing import Any


def clip_expression(ee_expression: str, geojson_geometry: dict[str, Any]) -> str:
    """Wrap an EE expression in Image.clip(region).

    This ensures edge tiles (which extend beyond the export region for
    pixel alignment) return nodata for out-of-bounds pixels instead of
    computing them. The original expression is untouched — we just
    compose it with a clip operation.

    Args:
        ee_expression: Serialized EE computation (JSON string).
        geojson_geometry: GeoJSON Polygon or MultiPolygon in WGS84.

    Returns:
        New serialized EE expression with clip applied.
    """
    original = json.loads(ee_expression)

    # EE Cloud API serialization uses a flat {result, values} structure where
    # "result" names the root node and "values" is a map of node-id → node.
    # To compose expressions we merge value maps and use valueReference to
    # point from the clip's "input" argument to the original expression's root.
    original_result = original["result"]
    original_values = original.get("values", {})

    # Pick keys for new nodes that don't collide with the original.
    used_keys = set(original_values.keys())

    def _next_key(prefix: str) -> str:
        candidate = f"{prefix}_0"
        i = 0
        while candidate in used_keys:
            i += 1
            candidate = f"{prefix}_{i}"
        used_keys.add(candidate)
        return candidate

    geom_key = _next_key("_geom")
    clip_key = _next_key("_clip")

    merged_values = {**original_values}

    # Image.clip expects an EE Geometry, not raw GeoJSON. Use
    # GeometryConstructors.Polygon (or .MultiPolygon) to construct a
    # proper EE geometry from the coordinate array.
    geom_type = geojson_geometry.get("type", "Polygon")
    if geom_type == "Polygon":
        constructor = "GeometryConstructors.Polygon"
    elif geom_type == "MultiPolygon":
        constructor = "GeometryConstructors.MultiPolygon"
    else:
        msg = f"Unsupported geometry type for clip: {geom_type}"
        raise ValueError(msg)

    merged_values[geom_key] = {
        "functionInvocationValue": {
            "functionName": constructor,
            "arguments": {
                "coordinates": {"constantValue": geojson_geometry["coordinates"]},
            },
        }
    }

    merged_values[clip_key] = {
        "functionInvocationValue": {
            "functionName": "Image.clip",
            "arguments": {
                "input": {"valueReference": original_result},
                "geometry": {"valueReference": geom_key},
            },
        }
    }

    clipped = {
        "result": clip_key,
        "values": merged_values,
    }

    return json.dumps(clipped)
