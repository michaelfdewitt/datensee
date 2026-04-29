"""Tile sampling strategies for validation checks.

Most checks don't need every tile. This module provides strategies to select
a representative subset for cost and speed.
"""

from __future__ import annotations

import random
from enum import StrEnum

from datensee.config import TileCoordinate


class SamplingStrategy(StrEnum):
    """How to select tiles for sampling-based checks."""

    ALL = "all"
    STRATIFIED = "stratified"
    RANDOM = "random"


def sample_tiles(
    tiles: list[TileCoordinate],
    *,
    strategy: SamplingStrategy = SamplingStrategy.STRATIFIED,
    n: int = 20,
    seed: int = 42,
) -> list[TileCoordinate]:
    """Select a subset of tiles according to the given strategy.

    Args:
        tiles: All tiles from the pipeline config.
        strategy: Sampling method.
        n: Maximum number of tiles to return (ignored for ALL).
        seed: RNG seed for deterministic sampling.

    Returns:
        Selected tiles (may be fewer than n if fewer tiles exist).
    """
    if not tiles:
        return []

    match strategy:
        case SamplingStrategy.ALL:
            return list(tiles)
        case SamplingStrategy.RANDOM:
            return _random_sample(tiles, n, seed)
        case SamplingStrategy.STRATIFIED:
            return _stratified_sample(tiles, n, seed)


def _random_sample(tiles: list[TileCoordinate], n: int, seed: int) -> list[TileCoordinate]:
    """Uniform random sample of n tiles."""
    if len(tiles) <= n:
        return list(tiles)
    rng = random.Random(seed)
    return rng.sample(tiles, n)


def _stratified_sample(tiles: list[TileCoordinate], n: int, seed: int) -> list[TileCoordinate]:
    """4 corners + random edge tiles + random interior, capped at n.

    Ensures spatial coverage: corners catch edge-of-grid bugs, edges catch
    boundary issues, interior catches the common case.
    """
    if len(tiles) <= n:
        return list(tiles)

    max_row = max(t.row for t in tiles)
    max_col = max(t.col for t in tiles)

    by_pos: dict[tuple[int, int], TileCoordinate] = {(t.row, t.col): t for t in tiles}

    selected: dict[tuple[int, int], TileCoordinate] = {}

    # 1. Corners (up to 4)
    corners = [
        (0, 0),
        (0, max_col),
        (max_row, 0),
        (max_row, max_col),
    ]
    for rc in corners:
        if rc in by_pos:
            selected[rc] = by_pos[rc]

    if len(selected) >= n:
        return list(selected.values())[:n]

    # 2. Edges — tiles on the boundary rows/cols
    edges = [t for t in tiles if (t.row in (0, max_row) or t.col in (0, max_col))]
    edges = [t for t in edges if (t.row, t.col) not in selected]

    rng = random.Random(seed)
    edge_budget = min(len(edges), max(0, (n - len(selected)) // 2))
    if edge_budget > 0:
        for t in rng.sample(edges, edge_budget):
            selected[(t.row, t.col)] = t

    # 3. Interior — fill remaining budget
    interior = [t for t in tiles if (t.row, t.col) not in selected]
    remaining = n - len(selected)
    if remaining > 0 and interior:
        for t in rng.sample(interior, min(len(interior), remaining)):
            selected[(t.row, t.col)] = t

    return list(selected.values())
