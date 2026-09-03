"""Snapshot pinning for serialized EE expressions.

Every export captures a single timestamp ``T`` at submit time and
rewrites all asset-load nodes in the serialized expression to pin to
``T``, ensuring parallel tile fetches across workers see a consistent
view of mutable assets. This prevents mid-job collection updates from
producing inconsistent tile rasters.

``T`` is specified in **Unix microseconds**: Earth Engine's load-constructor
``version`` argument is a 64-bit integer compared against the asset's
``system:version`` microsecond timestamp. Passing milliseconds or seconds
yields a ``400 "not found at version N"`` error; passing nanoseconds exceeds
the maximum safe integer range (``> 10^17``), triggering server-side integer
overflow and `gRPC INTERNAL` failures.

BigQuery queries cannot be pinned automatically through this mechanism.
``runBigQuery`` takes SQL text, requiring an explicit BigQuery
``FOR SYSTEM_TIME AS OF`` clause, which DatensEE validates before submission.
``loadBigQuery`` lacks point-in-time read support and is rejected.
"""

from __future__ import annotations

import json
import warnings
from datetime import UTC, datetime
from typing import Any

PINNABLE_LOAD_FUNCTIONS: frozenset[str] = frozenset(
    {
        "Image.load",
        "ImageCollection.load",
        "Feature.load",
        "Collection.loadTable",
    }
)

UNSUPPORTED_BQ_FUNCTIONS: frozenset[str] = frozenset(
    {
        "FeatureCollection.loadBigQuery",
    }
)

RUN_BIGQUERY_FUNCTION: str = "FeatureCollection.runBigQuery"

# Values above this threshold trigger Earth Engine's server-side integer overflow.
# A microsecond timestamp through year 5138 remains below this threshold,
# whereas nanosecond timestamps exceed it.
_MAX_SAFE_VERSION: int = 10**17


class BigQueryNotPinnedError(ValueError):
    """A BigQuery node in the expression cannot be safely snapshotted."""


class SnapshotTimeOutOfRangeError(ValueError):
    """A ``snapshot_time_micros`` value is outside the pinnable range."""


class UnpinnableLoadWarning(UserWarning):
    """The expression loads data through a mechanism pinning can't cover."""


def pin_expression(expression: str, snapshot_time_micros: int) -> str:
    """Return a copy of ``expression`` with all asset loads pinned to ``T``.

    Recurses through every node in the serialized JSON. At each
    ``functionInvocationValue`` it:

      * Injects (or preserves, if already present) a ``version``
        argument set to ``snapshot_time_micros`` for any of the
        :data:`PINNABLE_LOAD_FUNCTIONS`.
      * Raises :class:`BigQueryNotPinnedError` for any
        :data:`UNSUPPORTED_BQ_FUNCTIONS` call.
      * Raises :class:`BigQueryNotPinnedError` for
        ``FeatureCollection.runBigQuery`` whose SQL lacks
        ``FOR SYSTEM_TIME AS OF``.

    This operation is idempotent: re-pinning an expression with the same
    timestamp returns an identical result.

    Args:
        expression: Serialized EE computation as a JSON string (the
            output of ``ee.serializer.encode``).
        snapshot_time_micros: Unix microseconds stamped into every
            load node's ``version`` argument. Values above ``10^17`` are
            rejected due to server-side overflow limits.

    Returns:
        Re-serialized expression JSON string.

    Raises:
        BigQueryNotPinnedError: If a BigQuery node prevents pinning.
        SnapshotTimeOutOfRangeError: If ``snapshot_time_micros`` is
            outside the valid microsecond range.
        json.JSONDecodeError: If ``expression`` is not valid JSON.
    """
    if snapshot_time_micros > _MAX_SAFE_VERSION:
        raise SnapshotTimeOutOfRangeError(
            f"snapshot_time_micros={snapshot_time_micros} exceeds safe Earth "
            f"Engine version range (maximum {_MAX_SAFE_VERSION}). Values of this "
            "magnitude typically indicate nanosecond timestamps instead of "
            "microseconds and trigger the EE INTERNAL-crash error path. "
            "Divide by 1000 to convert legacy values."
        )
    if snapshot_time_micros <= 0:
        # Values <= 0 (including EE's -1 latest-version sentinel) bypass
        # snapshot consistency and are rejected.
        raise SnapshotTimeOutOfRangeError(
            f"snapshot_time_micros={snapshot_time_micros} must be a positive "
            "microsecond Unix timestamp. Negative values (including EE's -1 "
            "'latest' sentinel) would defeat snapshot pinning."
        )
    tree = json.loads(expression)
    pinned_tree = _pin_tree(tree, snapshot_time_micros)
    # Match `ee.serializer.encode()` minified JSON formatting.
    return json.dumps(pinned_tree, separators=(",", ":"))


def _transform_invocation(invocation: dict[str, Any], snapshot_time_micros: int) -> dict[str, Any]:
    name = invocation.get("functionName")
    if name in PINNABLE_LOAD_FUNCTIONS:
        args = dict(invocation.get("arguments") or {})
        if "version" not in args:
            args["version"] = {"constantValue": snapshot_time_micros}
        return {**invocation, "arguments": args}
    if isinstance(name, str) and "loadGeoTIFF" in name:
        warnings.warn(
            f"Expression calls {name!r}, which loads from GCS and cannot be "
            "pinned to a snapshot version. If the object is overwritten "
            "while the export runs, tiles may mix data from different "
            "versions. Ensure the GCS object is immutable for the duration "
            "of the export.",
            UnpinnableLoadWarning,
            stacklevel=4,
        )
        return invocation
    if name in UNSUPPORTED_BQ_FUNCTIONS:
        raise BigQueryNotPinnedError(
            f"Expression calls {name!r}, which has no point-in-time read "
            "mechanism. Refusing to submit; parallel tile fetches would see "
            "different rows if the source table updates mid-job. Materialize "
            "the BigQuery data into a stable Earth Engine asset first, then "
            "load that asset instead."
        )
    if name == RUN_BIGQUERY_FUNCTION:
        _require_for_system_time(invocation, snapshot_time_micros)
        return invocation
    return invocation


def _pin_tree(node: Any, snapshot_time_micros: int) -> Any:
    if isinstance(node, dict):
        result: dict[str, Any] = {}
        for k, v in node.items():
            if k == "functionInvocationValue" and isinstance(v, dict):
                transformed_inv = _transform_invocation(v, snapshot_time_micros)
                result[k] = {
                    inv_k: _pin_tree(inv_v, snapshot_time_micros)
                    for inv_k, inv_v in transformed_inv.items()
                }
            else:
                result[k] = _pin_tree(v, snapshot_time_micros)
        return result
    if isinstance(node, list):
        return [_pin_tree(item, snapshot_time_micros) for item in node]
    return node


def _require_for_system_time(invocation: dict, snapshot_time_micros: int) -> None:
    arguments = invocation.get("arguments") or {}
    query_node = arguments.get("query") or {}
    query = query_node.get("constantValue") if isinstance(query_node, dict) else None
    if isinstance(query, str) and "FOR SYSTEM_TIME AS OF" in query.upper():
        return
    timestamp = datetime.fromtimestamp(snapshot_time_micros / 1_000_000, tz=UTC)
    bq_literal = f"TIMESTAMP('{timestamp.isoformat()}')"
    raise BigQueryNotPinnedError(
        "Expression calls 'FeatureCollection.runBigQuery' but the SQL query "
        "does not contain a 'FOR SYSTEM_TIME AS OF' clause. BigQuery tables "
        "can update during a job; without time-travel pinning, parallel tile "
        "fetches may read different rows and produce a self-inconsistent "
        "output.\n\n"
        f"Add 'FOR SYSTEM_TIME AS OF {bq_literal}' after each table reference "
        "in your query and retry."
    )
