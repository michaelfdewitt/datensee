"""Eval catalog — definitions and registry for all DatensEE evals.

Each eval has an ID, name, description, cost tier, and pass criteria.
The catalog is the single source of truth used by CLI help, docs generation,
and the eval runner.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class EvalID(StrEnum):
    """Stable identifiers for each eval."""

    E01 = "E01"
    E02 = "E02"
    E03 = "E03"
    E04 = "E04"
    E05 = "E05"
    E06 = "E06"
    E07 = "E07"
    E08 = "E08"
    E09 = "E09"
    E10 = "E10"


class CostTier(StrEnum):
    """Whether an eval requires EE API calls."""

    ZERO_COST = "zero_cost"
    API_COST = "api_cost"


class EvalDefinition(BaseModel):
    """Metadata for a single eval."""

    id: EvalID
    name: str
    description: str
    pass_criteria: str
    cost_tier: CostTier
    runs_on_all_tiles: bool = False


_CATALOG: dict[EvalID, EvalDefinition] = {
    EvalID.E01: EvalDefinition(
        id=EvalID.E01,
        name="Tile File Integrity",
        description=(
            "Every expected tile file exists, has TIFF magic bytes, and is larger than 1 KB."
        ),
        pass_criteria="All non-failed tiles are valid TIFF files >1 KB",
        cost_tier=CostTier.ZERO_COST,
        runs_on_all_tiles=True,
    ),
    EvalID.E02: EvalDefinition(
        id=EvalID.E02,
        name="Tile Dimensions",
        description=(
            "Tile pixel dimensions match tile_size_pixels from config. "
            "Band count and data type match the output config."
        ),
        pass_criteria="Exact match for all sampled tiles",
        cost_tier=CostTier.ZERO_COST,
    ),
    EvalID.E03: EvalDefinition(
        id=EvalID.E03,
        name="Tile Geospatial Metadata",
        description="CRS matches config, affine transform origin matches tile coordinates.",
        pass_criteria="CRS match, origin within 1e-6",
        cost_tier=CostTier.ZERO_COST,
    ),
    EvalID.E04: EvalDefinition(
        id=EvalID.E04,
        name="Boundary Continuity",
        description="Adjacent tiles' shared edge pixels form a smooth continuation.",
        pass_criteria="Mean absolute difference < threshold",
        cost_tier=CostTier.ZERO_COST,
    ),
    EvalID.E05: EvalDefinition(
        id=EvalID.E05,
        name="VRT Completeness",
        description=(
            "mosaic.vrt references every tile with correct band count/type "
            "and correct overall dimensions."
        ),
        pass_criteria="All tiles referenced in VRT",
        cost_tier=CostTier.ZERO_COST,
        runs_on_all_tiles=True,
    ),
    EvalID.E06: EvalDefinition(
        id=EvalID.E06,
        name="VRT Spatial Correctness",
        description="VRT bounding box covers the entire export region.",
        pass_criteria="BBox covers input geometry",
        cost_tier=CostTier.ZERO_COST,
    ),
    EvalID.E07: EvalDefinition(
        id=EvalID.E07,
        name="Pixel Value Accuracy",
        description=(
            "For sampled tiles, re-fetch from EE HV API in NPY format and "
            "compare pixel values against the pipeline output."
        ),
        pass_criteria="Max absolute difference < epsilon",
        cost_tier=CostTier.API_COST,
    ),
    EvalID.E08: EvalDefinition(
        id=EvalID.E08,
        name="Failure Accounting",
        description=(
            "tiles_on_disk + tiles_in_failures == tiles_in_config. No tiles unaccounted for."
        ),
        pass_criteria="Set equality on (row, col)",
        cost_tier=CostTier.ZERO_COST,
        runs_on_all_tiles=True,
    ),
    EvalID.E09: EvalDefinition(
        id=EvalID.E09,
        name="Pixel Range Sanity",
        description=(
            "Sampled pixel values fall within expected range for the data type and expression."
        ),
        pass_criteria=">95% in range, <5% all-NaN tiles",
        cost_tier=CostTier.ZERO_COST,
    ),
    EvalID.E10: EvalDefinition(
        id=EvalID.E10,
        name="Output Size Plausibility",
        description=("Total output size is within 0.2x–5x of the cost estimator's prediction."),
        pass_criteria="Within bounds",
        cost_tier=CostTier.ZERO_COST,
        runs_on_all_tiles=True,
    ),
}


def get_eval(eval_id: EvalID) -> EvalDefinition:
    """Look up an eval definition by ID."""
    return _CATALOG[eval_id]


def list_evals() -> list[EvalDefinition]:
    """Return all eval definitions in ID order."""
    return [_CATALOG[eid] for eid in EvalID]


def zero_cost_evals() -> list[EvalID]:
    """Return IDs of all zero-cost evals."""
    return [eid for eid, defn in _CATALOG.items() if defn.cost_tier == CostTier.ZERO_COST]
