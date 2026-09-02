"""DatensEE: Parallelize Google Earth Engine exports via Cloud Dataflow."""

from datensee._version import __version__  # noqa: F401

# Public API — usable from notebooks and scripts without touching the CLI.
from datensee.api import ExportResult, demo, export, poll, tile  # noqa: F401

# Grid types for the exact-grid export path (crs + transform + dimensions).
from datensee.config import AffineTransform, GridDimensions, PixelGrid  # noqa: F401, E402
