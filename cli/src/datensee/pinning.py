"""Snapshot pinning for serialized EE expressions.

Every export captures a single timestamp ``T`` at submit time and
rewrites all asset-load nodes in the serialized expression to pin to
``T``, so parallel tile fetches across thousands of workers see a
consistent view of mutable assets. Without this, a collection update
mid-job would let tile A see the new version while tile B sees the old
one — silently producing a self-inconsistent COG.

``T`` is **Unix microseconds**: EE's load-constructor ``version``
argument is a Long that EE compares against the asset's
``system:version`` field, which is a microsecond Unix timestamp.
Passing milliseconds or seconds yields a clean ``400 "not found at
version N"``; passing **nanoseconds** lands in a value range
(``> ~1e17``) where EE's internal version comparison overflows and
crashes with ``gRPC INTERNAL`` — which typically does *not* surface
to the client and instead manifests as a silent 25–90 s hang. If
you ever change the units here, also adjust the boundary tests in
``cli/tests/test_pinning.py`` that fail any value above ~1e17 — those
guard against regressing to the nanosecond bug.

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

# Values above this threshold reach EE's INTERNAL-crash code path
# (verified against LANDSAT/LC09/C02/T1_L2 on 2026-05-13). A
# microsecond timestamp for any date through year ~5138 is below this;
# nanoseconds for any date after ~1973 is above. We refuse to emit a
# value in the danger zone — better a clear Python-side error than a
# silent 90 s hang for the user.
_MAX_SAFE_VERSION: int = 10**17


class BigQueryNotPinnedError(ValueError):
    """A BigQuery node in the expression cannot be safely snapshotted."""


class SnapshotTimeOutOfRangeError(ValueError):
    """A ``snapshot_time_micros`` value is in EE's INTERNAL-crash range."""


def pin_expression(expression: str, snapshot_time_micros: int) -> str:
    """Return a copy of ``expression`` with all asset loads pinned to ``T``.

    Recurses through every node in the serialized JSON. At each
    ``functionInvocationValue`` it:

      * Injects (or leaves alone, if already present) a ``version``
        argument set to ``snapshot_time_micros`` for any of the
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
        snapshot_time_micros: Unix microseconds. Stamped into every
            load node's ``version`` argument. Values above ``1e17`` are
            rejected — they fall in EE's INTERNAL-crash range (the
            unit-confusion bug that motivated this guard).

    Returns:
        Re-serialized expression JSON string.

    Raises:
        BigQueryNotPinnedError: If a BigQuery node prevents pinning.
        SnapshotTimeOutOfRangeError: If ``snapshot_time_micros`` is
            above EE's safe range — typically because the caller still
            has a nanosecond value from before this fix.
        json.JSONDecodeError: If ``expression`` is not valid JSON.
    """
    if snapshot_time_micros > _MAX_SAFE_VERSION:
        raise SnapshotTimeOutOfRangeError(
            f"snapshot_time_micros={snapshot_time_micros} is in EE's "
            "INTERNAL-crash range. Expected microsecond Unix timestamp "
            f"(<= {_MAX_SAFE_VERSION}); got a value 1000x too large, "
            "consistent with a nanosecond timestamp. Divide by 1000 if "
            "you carried this from a pre-fix `_export_meta.json`."
        )
    tree = json.loads(expression)
    _pin_in_place(tree, snapshot_time_micros)
    # Match `ee.serializer.encode()`'s minified style — keeps the wire
    # payload small and lets byte-equal expression comparisons survive
    # a no-op pin pass.
    return json.dumps(tree, separators=(",", ":"))


def _pin_in_place(node: Any, snapshot_time_micros: int) -> None:
    if isinstance(node, dict):
        invocation = node.get("functionInvocationValue")
        if isinstance(invocation, dict):
            _maybe_pin_invocation(invocation, snapshot_time_micros)
        for value in node.values():
            _pin_in_place(value, snapshot_time_micros)
    elif isinstance(node, list):
        for item in node:
            _pin_in_place(item, snapshot_time_micros)


def _maybe_pin_invocation(invocation: dict, snapshot_time_micros: int) -> None:
    name = invocation.get("functionName")
    if name in PINNABLE_LOAD_FUNCTIONS:
        args = invocation.setdefault("arguments", {})
        args.setdefault("version", {"constantValue": snapshot_time_micros})
    elif name in UNSUPPORTED_BQ_FUNCTIONS:
        raise BigQueryNotPinnedError(
            f"Expression calls {name!r}, which has no point-in-time read "
            "mechanism. Refusing to submit; parallel tile fetches would see "
            "different rows if the source table updates mid-job. Materialize "
            "the BigQuery data into a stable Earth Engine asset first, then "
            "load that asset instead."
        )
    elif name == RUN_BIGQUERY_FUNCTION:
        _require_for_system_time(invocation, snapshot_time_micros)


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
