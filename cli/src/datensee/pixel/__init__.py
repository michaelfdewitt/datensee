"""Pixel pipeline: raster-shaped subpackage for the EE `computePixels` path.

This subpackage contains modules tied to the pixel-grid
model: tile decomposition, COG output, raster validation, the quadtree
retry splitter, and raster configuration models.

The seam exists so a future vector pipeline (``computeFeatures`` →
zonal stats → GeoParquet) can land as a sibling subpackage without
disturbing the pixel surface, and so a reader of either subpackage
sees only the code that's relevant to it.
"""

from __future__ import annotations
