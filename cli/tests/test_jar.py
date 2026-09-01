"""Tests for datensee.jar — pipeline JAR discovery and management."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from datensee.jar import (
    JAR_FILENAME,
    download_jar,
    ensure_jar,
    find_jar,
    jar_path,
    versioned_jar_filename,
)


class TestFindJar:
    def test_explicit_path_found(self, tmp_path: Path) -> None:
        jar = tmp_path / JAR_FILENAME
        jar.write_bytes(b"fake jar")
        assert find_jar(jar) == jar

    def test_explicit_path_missing_raises(self, tmp_path: Path) -> None:
        jar = tmp_path / "nonexistent.jar"
        with pytest.raises(FileNotFoundError, match="explicit path"):
            find_jar(jar)

    def test_env_var_found(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        jar = tmp_path / JAR_FILENAME
        jar.write_bytes(b"fake jar")
        monkeypatch.setenv("DATENSEE_JAR", str(jar))
        assert find_jar() == jar

    def test_env_var_missing_file_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DATENSEE_JAR", str(tmp_path / "nope.jar"))
        with pytest.raises(FileNotFoundError, match="DATENSEE_JAR"):
            find_jar()

    def test_cache_dir_found(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DATENSEE_JAR", raising=False)
        cache_dir = tmp_path / ".datensee" / "jars"
        cache_dir.mkdir(parents=True)
        jar = cache_dir / JAR_FILENAME
        jar.write_bytes(b"fake jar")
        monkeypatch.setattr("datensee.jar._CACHE_DIR", cache_dir)
        assert find_jar() == jar

    def test_versioned_cache_wins_over_unversioned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DATENSEE_JAR", raising=False)
        cache_dir = tmp_path / "jars"
        cache_dir.mkdir()
        versioned = cache_dir / versioned_jar_filename()
        versioned.write_bytes(b"release build")
        (cache_dir / JAR_FILENAME).write_bytes(b"dev build")
        monkeypatch.setattr("datensee.jar._CACHE_DIR", cache_dir)
        assert find_jar() == versioned

    def test_no_jar_anywhere_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DATENSEE_JAR", raising=False)
        monkeypatch.setattr("datensee.jar._CACHE_DIR", tmp_path / "empty_cache")
        monkeypatch.setattr("datensee.jar._REPO_JAR", tmp_path / "nonexistent.jar")
        with pytest.raises(FileNotFoundError, match="datensee jar download"):
            find_jar()


class TestJarPath:
    def test_returns_none_when_not_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DATENSEE_JAR", raising=False)
        monkeypatch.setattr("datensee.jar._CACHE_DIR", tmp_path / "empty")
        monkeypatch.setattr("datensee.jar._REPO_JAR", tmp_path / "nope.jar")
        assert jar_path() is None

    def test_returns_path_when_found(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        jar = tmp_path / JAR_FILENAME
        jar.write_bytes(b"fake jar")
        monkeypatch.setenv("DATENSEE_JAR", str(jar))
        assert jar_path() == jar


@pytest.fixture
def empty_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """No JAR anywhere in the search path; cache dir points into tmp."""
    monkeypatch.delenv("DATENSEE_JAR", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("DATENSEE_GITHUB_TOKEN", raising=False)
    cache = tmp_path / "jars"
    monkeypatch.setattr("datensee.jar._CACHE_DIR", cache)
    monkeypatch.setattr("datensee.jar._REPO_JAR", tmp_path / "nope.jar")
    return cache


class TestDownloadJar:
    def test_downloads_release_asset_into_versioned_cache(
        self, empty_cache: Path, httpx_mock: object
    ) -> None:
        from pytest_httpx import HTTPXMock

        assert isinstance(httpx_mock, HTTPXMock)
        httpx_mock.add_response(
            url="https://github.com/michaelfdewitt/datensee/releases/download/v9.9.9/"
            + JAR_FILENAME,
            content=b"PK\x03\x04 fake jar",
        )
        path = download_jar("9.9.9")
        assert path == empty_cache / "datensee-pipeline-9.9.9.jar"
        assert path.read_bytes() == b"PK\x03\x04 fake jar"
        assert not list(empty_cache.glob("*.tmp"))

    def test_cached_download_is_not_refetched(self, empty_cache: Path) -> None:
        empty_cache.mkdir()
        cached = empty_cache / "datensee-pipeline-9.9.9.jar"
        cached.write_bytes(b"cached")
        # No httpx_mock fixture → any network call would raise.
        assert download_jar("9.9.9") == cached

    def test_missing_release_is_actionable(self, empty_cache: Path, httpx_mock: object) -> None:
        from pytest_httpx import HTTPXMock

        assert isinstance(httpx_mock, HTTPXMock)
        httpx_mock.add_response(status_code=404)
        with pytest.raises(FileNotFoundError, match="releases/tag/v9.9.9") as excinfo:
            download_jar("9.9.9")
        assert "datensee jar build" in str(excinfo.value)
        assert not list(empty_cache.glob("*"))

    def test_partial_download_is_discarded(self, empty_cache: Path, httpx_mock: object) -> None:
        from pytest_httpx import HTTPXMock

        assert isinstance(httpx_mock, HTTPXMock)
        httpx_mock.add_exception(httpx.ReadTimeout("slow"))
        with pytest.raises(httpx.ReadTimeout):
            download_jar("9.9.9")
        assert not list(empty_cache.glob("*"))

    def test_private_repo_uses_api_resolved_url(
        self, empty_cache: Path, monkeypatch: pytest.MonkeyPatch, httpx_mock: object
    ) -> None:
        from pytest_httpx import HTTPXMock

        assert isinstance(httpx_mock, HTTPXMock)
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
        api = "https://api.github.com/repos/michaelfdewitt/datensee"
        httpx_mock.add_response(
            url=f"{api}/releases/tags/v9.9.9",
            match_headers={"Authorization": "Bearer ghp_secret"},
            json={"assets": [{"id": 42, "name": JAR_FILENAME}]},
        )
        httpx_mock.add_response(
            url=f"{api}/releases/assets/42",
            match_headers={"Authorization": "Bearer ghp_secret"},
            status_code=302,
            headers={"location": "https://objects.example/signed"},
        )
        # The pre-signed URL must be fetched *without* the bearer token.
        httpx_mock.add_response(url="https://objects.example/signed", content=b"jar")
        path = download_jar("9.9.9")
        assert path.read_bytes() == b"jar"
        signed = [r for r in httpx_mock.get_requests() if r.url.host == "objects.example"]
        assert signed and "authorization" not in signed[0].headers


class TestEnsureJar:
    def test_returns_existing_without_download(
        self, empty_cache: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        empty_cache.mkdir()
        jar = empty_cache / JAR_FILENAME
        jar.write_bytes(b"dev build")
        monkeypatch.setattr(
            "datensee.jar.download_jar", lambda *a, **k: pytest.fail("should not download")
        )
        assert ensure_jar() == jar

    def test_downloads_when_absent(
        self, empty_cache: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        downloaded = empty_cache / versioned_jar_filename()
        monkeypatch.setattr("datensee.jar.download_jar", lambda *a, **k: downloaded)
        assert ensure_jar() == downloaded
