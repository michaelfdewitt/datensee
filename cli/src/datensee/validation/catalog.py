"""Eval catalog — definitions and registry for all DatensEE checks.

Each check has an ID, name, description, cost tier, and pass criteria.
The catalog is the single source of truth used by CLI help, docs generation,
and the check runner.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class CheckID(StrEnum):
    """Stable identifiers for each check.

    Note: E05 / E06 (VRT-related) were removed when the pipeline stopped
    producing a VRT manifest; output is now a directory of COGs only,
    sized via ``output_tile_size_pixels``. The IDs are kept as gaps for
    stability of any historic JSON reports rather than reused.
    """

    E01 = "E01"
    E02 = "E02"
    E03 = "E03"
    E04 = "E04"
    E07 = "E07"
    E08 = "E08"
    E09 = "E09"
    E10 = "E10"


class CostTier(StrEnum):
    """Whether an check requires EE API calls."""

    ZERO_COST = "zero_cost"
    API_COST = "api_cost"


class CheckDefinition(BaseModel):
    """Metadata for a single check."""

    id: CheckID
    name: str
    description: str
    pass_criteria: str
    cost_tier: CostTier
    runs_on_all_tiles: bool = False


_CATALOG: dict[CheckID, CheckDefinition] = {
    CheckID.E01: CheckDefinition(
        id=CheckID.E01,
        name="Tile File Integrity",
        description=(
            "Every expected tile file exists, has TIFF magic bytes, and is larger than 1 KB."
        ),
        pass_criteria="All non-failed tiles are valid TIFF files >1 KB",
        cost_tier=CostTier.ZERO_COST,
        runs_on_all_tiles=True,
    ),
    CheckID.E02: CheckDefinition(
        id=CheckID.E02,
        name="Tile Dimensions",
        description=(
            "Tile pixel dimensions match tile_size_pixels from config. "
            "Band count and data type match the output config."
        ),
        pass_criteria="Exact match for all sampled tiles",
        cost_tier=CostTier.ZERO_COST,
    ),
    CheckID.E03: CheckDefinition(
        id=CheckID.E03,
        name="Tile Geospatial Metadata",
        description="CRS matches config, affine transform origin matches tile coordinates.",
        pass_criteria="CRS match, origin within 1e-6",
        cost_tier=CostTier.ZERO_COST,
    ),
    CheckID.E04: CheckDefinition(
        id=CheckID.E04,
        name="Boundary Continuity",
        description="Adjacent tiles' shared edge pixels form a smooth continuation.",
        pass_criteria="Mean absolute difference < threshold",
        cost_tier=CostTier.ZERO_COST,
    ),
    CheckID.E07: CheckDefinition(
        id=CheckID.E07,
        name="Pixel Value Accuracy",
        description=(
            "For sampled tiles, re-fetch from EE HV API in NPY format and "
            "compare pixel values against the pipeline output."
        ),
        pass_criteria="Max absolute difference < epsilon",
        cost_tier=CostTier.API_COST,
    ),
    CheckID.E08: CheckDefinition(
        id=CheckID.E08,
        name="Failure Accounting",
        description=(
            "tiles_on_disk + tiles_in_failures == tiles_in_config. No tiles unaccounted for."
        ),
        pass_criteria="Set equality on (row, col)",
        cost_tier=CostTier.ZERO_COST,
        runs_on_all_tiles=True,
    ),
    CheckID.E09: CheckDefinition(
        id=CheckID.E09,
        name="Pixel Range Sanity",
        description=(
            "Sampled pixel values fall within expected range for the data type and expression."
        ),
        pass_criteria=">95% in range, <5% all-NaN tiles",
        cost_tier=CostTier.ZERO_COST,
    ),
    CheckID.E10: CheckDefinition(
        id=CheckID.E10,
        name="Output Size Plausibility",
        description=("Total output size is within 0.2x–5x of the cost estimator's prediction."),
        pass_criteria="Within bounds",
        cost_tier=CostTier.ZERO_COST,
        runs_on_all_tiles=True,
    ),
}


def get_check(check_id: CheckID) -> CheckDefinition:
    """Look up an check definition by ID."""
    return _CATALOG[check_id]


def list_checks() -> list[CheckDefinition]:
    """Return all check definitions in ID order."""
    return [_CATALOG[eid] for eid in CheckID]


def zero_cost_checks() -> list[CheckID]:
    """Return IDs of all zero-cost checks."""
    return [eid for eid, defn in _CATALOG.items() if defn.cost_tier == CostTier.ZERO_COST]
