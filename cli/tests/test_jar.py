"""Tests for datensee.jar — pipeline JAR discovery and management."""

from __future__ import annotations

from pathlib import Path

import pytest

from datensee.jar import JAR_FILENAME, find_jar, jar_path


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

    def test_no_jar_anywhere_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DATENSEE_JAR", raising=False)
        monkeypatch.setattr("datensee.jar._CACHE_DIR", tmp_path / "empty_cache")
        monkeypatch.setattr("datensee.jar._REPO_JAR", tmp_path / "nonexistent.jar")
        with pytest.raises(FileNotFoundError, match="Pipeline JAR not found"):
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
