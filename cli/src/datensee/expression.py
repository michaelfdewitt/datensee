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

    clipped = {
        "result": "0",
        "values": {
            "0": {
                "functionInvocationValue": {
                    "functionName": "Image.clip",
                    "arguments": {
                        "input": original,
                        "geometry": {"constantValue": geojson_geometry},
                    },
                }
            }
        },
    }

    return json.dumps(clipped)
