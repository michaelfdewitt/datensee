"""Pin every version-derived artifact to the one declared in pyproject.toml.

The package version is declared exactly once (``cli/pyproject.toml``).
``datensee.__version__``, the default Flex Template spec URI, the GitHub
Release the local-mode JAR is fetched from, and the Gradle ``version`` all
derive from it. These tests fail if a derivation drifts — or if the
installed metadata is stale relative to the source tree (re-run
``uv sync``).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from datensee import __version__
from datensee.jar import versioned_jar_filename
from datensee.template import TEMPLATE_BUCKET, resolve_template_spec

_CLI_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _CLI_ROOT.parent


@pytest.fixture(scope="module")
def declared_version() -> str:
    with (_CLI_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]["version"]


def test_runtime_version_matches_pyproject(declared_version: str) -> None:
    assert __version__ == declared_version


def test_template_spec_is_pinned_to_version(
    declared_version: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATENSEE_TEMPLATE_SPEC", raising=False)
    assert resolve_template_spec() == (f"gs://{TEMPLATE_BUCKET}/v{declared_version}/datensee.json")


def test_jar_cache_name_is_pinned_to_version(declared_version: str) -> None:
    assert versioned_jar_filename() == f"datensee-pipeline-{declared_version}.jar"


def test_gradle_reads_version_from_pyproject() -> None:
    gradle = _REPO_ROOT / "pipelines" / "build.gradle.kts"
    if not gradle.exists():
        pytest.skip("not a source checkout")
    text = gradle.read_text()
    assert 'file("../cli/pyproject.toml")' in text
    assert not any(line.strip().startswith('version = "') for line in text.splitlines()), (
        "Gradle version must be read from pyproject.toml, not hard-coded"
    )
