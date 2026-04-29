"""Snapshot pinning for serialized EE expressions.

Every export captures a single timestamp ``T`` at submit time and
rewrites all asset-load nodes in the serialized expression to pin to
``T``, so parallel tile fetches across thousands of workers see a
consistent view of mutable assets. Without this, a collection update
mid-job would let tile A see the new version while tile B sees the old
one — silently producing a self-inconsistent COG.

``T`` is Unix nanoseconds: EE's load-constructor ``version`` argument
accepts a nanosecond-precision long, and that's the granularity that
lines up with EE's asset-version timeline.

BigQuery loads can't be pinned through this mechanism. ``runBigQuery``
takes opaque user SQL — the only safe pin is BigQuery's
``FOR SYSTEM_TIME AS OF`` clause, which the user must add themselves;
we refuse to submit an expression whose query is missing it.
``loadBigQuery`` has no point-in-time read mechanism at all and is
unconditionally rejected.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

PINNABLE_LOAD_FUNCTIONS: frozenset[str] = frozenset({
    "Image.load",
    "ImageCollection.load",
    "Feature.load",
    "Collection.loadTable",
})

UNSUPPORTED_BQ_FUNCTIONS: frozenset[str] = frozenset({
    "FeatureCollection.loadBigQuery",
})

RUN_BIGQUERY_FUNCTION: str = "FeatureCollection.runBigQuery"


class BigQueryNotPinnedError(ValueError):
    """A BigQuery node in the expression cannot be safely snapshotted."""


def pin_expression(expression: str, snapshot_time_nanos: int) -> str:
    """Return a copy of ``expression`` with all asset loads pinned to ``T``.

    Recurses through every node in the serialized JSON. At each
    ``functionInvocationValue`` it:

      * Injects (or leaves alone, if already present) a ``version``
        argument set to ``snapshot_time_nanos`` for any of the
        :data:`PINNABLE_LOAD_FUNCTIONS`.
      * Raises :class:`BigQueryNotPinnedError` for any
        :data:`UNSUPPORTED_BQ_FUNCTIONS` call.
      * Raises :class:`BigQueryNotPinnedError` for
        ``FeatureCollection.runBigQuery`` whose SQL lacks
        ``FOR SYSTEM_TIME AS OF``.

    Idempotent — re-pinning a previously pinned expression with the
    same ``T`` leaves it unchanged.

    Args:
        expression: Serialized EE computation as a JSON string (the
            output of ``ee.serializer.encode``).
        snapshot_time_nanos: Unix nanoseconds. Stamped into every load
            node's ``version`` argument.

    Returns:
        Re-serialized expression JSON string.

    Raises:
        BigQueryNotPinnedError: If a BigQuery node prevents pinning.
        json.JSONDecodeError: If ``expression`` is not valid JSON.
    """
    tree = json.loads(expression)
    _pin_in_place(tree, snapshot_time_nanos)
    # Match `ee.serializer.encode()`'s minified style — keeps the wire
    # payload small and lets byte-equal expression comparisons survive
    # a no-op pin pass.
    return json.dumps(tree, separators=(",", ":"))


def _pin_in_place(node: Any, snapshot_time_nanos: int) -> None:
    if isinstance(node, dict):
        invocation = node.get("functionInvocationValue")
        if isinstance(invocation, dict):
            _maybe_pin_invocation(invocation, snapshot_time_nanos)
        for value in node.values():
            _pin_in_place(value, snapshot_time_nanos)
    elif isinstance(node, list):
        for item in node:
            _pin_in_place(item, snapshot_time_nanos)


def _maybe_pin_invocation(invocation: dict, snapshot_time_nanos: int) -> None:
    name = invocation.get("functionName")
    if name in PINNABLE_LOAD_FUNCTIONS:
        args = invocation.setdefault("arguments", {})
        args.setdefault("version", {"constantValue": snapshot_time_nanos})
    elif name in UNSUPPORTED_BQ_FUNCTIONS:
        raise BigQueryNotPinnedError(
            f"Expression calls {name!r}, which has no point-in-time read "
            "mechanism. Refusing to submit; parallel tile fetches would see "
            "different rows if the source table updates mid-job. Materialize "
            "the BigQuery data into a stable Earth Engine asset first, then "
            "load that asset instead."
        )
    elif name == RUN_BIGQUERY_FUNCTION:
        _require_for_system_time(invocation, snapshot_time_nanos)


def _require_for_system_time(invocation: dict, snapshot_time_nanos: int) -> None:
    arguments = invocation.get("arguments") or {}
    query_node = arguments.get("query") or {}
    query = query_node.get("constantValue") if isinstance(query_node, dict) else None
    if isinstance(query, str) and "FOR SYSTEM_TIME AS OF" in query.upper():
        return
    timestamp = datetime.fromtimestamp(snapshot_time_nanos / 1_000_000_000, tz=UTC)
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
