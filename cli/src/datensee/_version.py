"""Single source of truth for the package version at runtime.

The version is declared once, in ``cli/pyproject.toml``; everything else
derives from it: ``datensee.__version__``, the default Flex Template
spec URI (``datensee.template``), the GitHub Release the pipeline JAR is
downloaded from (``datensee.jar``), and the Gradle ``version`` (read
from the same TOML by ``pipelines/build.gradle.kts``).
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("datensee")
except PackageNotFoundError:  # source tree on sys.path without an install
    __version__ = "0.0.0+unknown"
