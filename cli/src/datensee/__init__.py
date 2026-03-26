"""DatensEE: Parallelize Google Earth Engine exports via Cloud Dataflow."""

__version__ = "0.1.0-dev"

# Public API — usable from notebooks and scripts without touching the CLI.
from datensee.api import ExportResult, demo, export, poll, tile  # noqa: F401
