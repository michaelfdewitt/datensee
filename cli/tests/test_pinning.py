"""Unit tests for snapshot pinning of EE expressions."""

from __future__ import annotations

import json

import pytest

from datensee.pinning import (
    BigQueryNotPinnedError,
    pin_expression,
)

T = 1_700_000_000_123_456_789  # arbitrary fixed Unix nanos


def _wrap(node: dict) -> str:
    """Wrap a single value node in EE's outer expression envelope."""
    return json.dumps({"result": "0", "values": {"0": node}})


def _load_node(function_name: str, **arg_constants: object) -> dict:
    """Build a `functionInvocationValue` for a load function with constant args."""
    arguments = {key: {"constantValue": value} for key, value in arg_constants.items()}
    return {
        "functionInvocationValue": {
            "functionName": function_name,
            "arguments": arguments,
        }
    }


def _values(tree: str) -> dict:
    return json.loads(tree)["values"]


def _invocation(tree: str, value_id: str = "0") -> dict:
    return _values(tree)[value_id]["functionInvocationValue"]


# ---------------------------------------------------------------------------
# Basic pinning
# ---------------------------------------------------------------------------


def test_pins_image_load_when_version_absent() -> None:
    expression = _wrap(_load_node("Image.load", id="USGS/SRTMGL1_003"))
    pinned = pin_expression(expression, T)
    args = _invocation(pinned)["arguments"]
    assert args["version"] == {"constantValue": T}
    assert args["id"] == {"constantValue": "USGS/SRTMGL1_003"}


def test_pins_image_collection_load() -> None:
    expression = _wrap(_load_node("ImageCollection.load", id="LANDSAT/LC09/C02/T1_L2"))
    pinned = pin_expression(expression, T)
    assert _invocation(pinned)["arguments"]["version"] == {"constantValue": T}


def test_pins_collection_load_table() -> None:
    expression = _wrap(
        _load_node("Collection.loadTable", tableId="USDOS/LSIB_SIMPLE/2017")
    )
    pinned = pin_expression(expression, T)
    assert _invocation(pinned)["arguments"]["version"] == {"constantValue": T}


def test_pins_feature_load() -> None:
    expression = _wrap(_load_node("Feature.load", id="users/foo/some_feature"))
    pinned = pin_expression(expression, T)
    assert _invocation(pinned)["arguments"]["version"] == {"constantValue": T}


# ---------------------------------------------------------------------------
# Idempotence + non-clobbering
# ---------------------------------------------------------------------------


def test_pinning_is_idempotent() -> None:
    expression = _wrap(_load_node("Image.load", id="USGS/SRTMGL1_003"))
    once = pin_expression(expression, T)
    twice = pin_expression(once, T)
    assert once == twice


def test_existing_version_arg_is_preserved() -> None:
    user_pinned_T = 1_500_000_000_000_000_000
    node = _load_node("Image.load", id="USGS/SRTMGL1_003")
    node["functionInvocationValue"]["arguments"]["version"] = {
        "constantValue": user_pinned_T
    }
    expression = _wrap(node)
    pinned = pin_expression(expression, T)
    assert _invocation(pinned)["arguments"]["version"] == {
        "constantValue": user_pinned_T
    }


# ---------------------------------------------------------------------------
# Nested + multiple loads
# ---------------------------------------------------------------------------


def test_multiple_loads_all_get_pinned() -> None:
    """Two siblings under a binary op — both must be pinned."""
    nested = {
        "functionInvocationValue": {
            "functionName": "Image.add",
            "arguments": {
                "image1": _load_node("Image.load", id="users/foo/a"),
                "image2": _load_node("Image.load", id="users/foo/b"),
            },
        }
    }
    pinned = pin_expression(_wrap(nested), T)
    args = _values(pinned)["0"]["functionInvocationValue"]["arguments"]
    inner_a = args["image1"]["functionInvocationValue"]["arguments"]
    inner_b = args["image2"]["functionInvocationValue"]["arguments"]
    assert inner_a["version"] == {"constantValue": T}
    assert inner_b["version"] == {"constantValue": T}


def test_demo_expression_image_collection_gets_pinned() -> None:
    """Real fixture — bundled NDVI demo references LANDSAT/LC09."""
    from datensee.api import demo_expression

    pinned_json = pin_expression(demo_expression(), T)
    pinned_tree = json.loads(pinned_json)

    found_pinned_load = False

    def visit(node: object) -> None:
        nonlocal found_pinned_load
        if isinstance(node, dict):
            invocation = node.get("functionInvocationValue")
            if isinstance(invocation, dict):
                name = invocation.get("functionName")
                if name == "ImageCollection.load":
                    args = invocation.get("arguments", {})
                    assert args.get("version") == {"constantValue": T}
                    found_pinned_load = True
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(pinned_tree)
    assert found_pinned_load, "demo expression should contain at least one ImageCollection.load"


def test_flat_form_with_multiple_top_level_values() -> None:
    """EE also serializes expressions with sibling top-level value entries."""
    flat = json.dumps(
        {
            "result": "0",
            "values": {
                "0": _load_node("Image.load", id="users/foo/a"),
                "1": _load_node("ImageCollection.load", id="LANDSAT/LC09/C02/T1_L2"),
            },
        }
    )
    pinned = pin_expression(flat, T)
    values = _values(pinned)
    assert values["0"]["functionInvocationValue"]["arguments"]["version"] == {
        "constantValue": T
    }
    assert values["1"]["functionInvocationValue"]["arguments"]["version"] == {
        "constantValue": T
    }


# ---------------------------------------------------------------------------
# BigQuery guards
# ---------------------------------------------------------------------------


def test_load_bigquery_is_unconditionally_rejected() -> None:
    expression = _wrap(_load_node("FeatureCollection.loadBigQuery", table="proj.ds.tbl"))
    with pytest.raises(BigQueryNotPinnedError, match="loadBigQuery"):
        pin_expression(expression, T)


def test_run_bigquery_without_for_system_time_is_rejected() -> None:
    expression = _wrap(
        _load_node(
            "FeatureCollection.runBigQuery",
            query="SELECT geom, value FROM proj.ds.tbl",
        )
    )
    with pytest.raises(BigQueryNotPinnedError) as exc:
        pin_expression(expression, T)
    # The error message should hand the user the BQ literal to paste.
    assert "FOR SYSTEM_TIME AS OF" in str(exc.value)
    assert "TIMESTAMP(" in str(exc.value)


def test_run_bigquery_with_for_system_time_passes() -> None:
    sql = (
        "SELECT geom, value FROM proj.ds.tbl "
        "FOR SYSTEM_TIME AS OF TIMESTAMP('2026-04-30T00:00:00Z')"
    )
    expression = _wrap(
        _load_node("FeatureCollection.runBigQuery", query=sql)
    )
    # Must not raise — the user's SQL already pins.
    pinned = pin_expression(expression, T)
    # Query should pass through unchanged.
    assert _invocation(pinned)["arguments"]["query"] == {"constantValue": sql}


def test_run_bigquery_match_is_case_insensitive() -> None:
    sql = "select * from proj.ds.tbl for system_time as of timestamp('2026-04-30')"
    expression = _wrap(
        _load_node("FeatureCollection.runBigQuery", query=sql)
    )
    pin_expression(expression, T)  # must not raise


# ---------------------------------------------------------------------------
# No-op cases
# ---------------------------------------------------------------------------


def test_expression_without_loads_is_unchanged() -> None:
    expression = _wrap(
        {
            "functionInvocationValue": {
                "functionName": "Number.add",
                "arguments": {
                    "left": {"constantValue": 1},
                    "right": {"constantValue": 2},
                },
            }
        }
    )
    pinned = pin_expression(expression, T)
    assert json.loads(pinned) == json.loads(expression)


def test_invalid_json_propagates() -> None:
    with pytest.raises(json.JSONDecodeError):
        pin_expression("not valid json {", T)
