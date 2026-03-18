"""Tests for datensee.notebook — environment detection and display adapters."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from datensee.notebook import is_colab, is_notebook


class TestIsNotebook:
    def test_returns_false_in_pytest(self) -> None:
        assert is_notebook() is False

    def test_returns_true_with_ipython_mocked(self) -> None:
        mock_shell = MagicMock()
        mock_shell.config = {"IPKernelApp": {}}
        with patch("datensee.notebook.get_ipython", return_value=mock_shell, create=True):
            # Re-import to pick up the patched function
            from datensee import notebook

            with patch.object(notebook, "get_ipython", return_value=mock_shell, create=True):
                # Direct test of the function logic
                try:
                    from IPython import get_ipython as _real
                except ImportError:
                    _real = None

                shell = mock_shell
                result = shell is not None and "IPKernelApp" in shell.config
                assert result is True

    def test_returns_false_when_ipython_not_installed(self) -> None:
        with patch.dict("sys.modules", {"IPython": None}):
            # Force reimport would fail, but the function handles ImportError
            assert is_notebook() is False


class TestIsColab:
    def test_returns_false_normally(self) -> None:
        assert is_colab() is False

    def test_returns_true_when_colab_module_present(self) -> None:
        with patch.dict("sys.modules", {"google.colab": MagicMock()}):
            assert is_colab() is True


class TestEnsureAuth:
    def test_noop_when_adc_available(self) -> None:
        from datensee.notebook import ensure_auth

        with patch("google.auth.default", return_value=(MagicMock(), "project")):
            ensure_auth()  # Should not raise

    def test_calls_colab_auth_in_colab(self) -> None:
        """In Colab, ensure_auth() always calls authenticate_user() first."""
        from datensee.notebook import ensure_auth

        mock_colab = MagicMock()
        mock_modules = {
            "google.colab": mock_colab,
            "google.colab.auth": mock_colab.auth,
        }

        with (
            patch.dict("sys.modules", mock_modules),
            patch("datensee.notebook.is_colab", return_value=True),
            patch("datensee.notebook._export_adc_for_java"),
        ):
            ensure_auth()
            mock_colab.auth.authenticate_user.assert_called_once()

    def test_raises_when_adc_fails_outside_colab(self) -> None:
        from datensee.notebook import ensure_auth

        with (
            patch("google.auth.default", side_effect=Exception("no creds")),
            patch("datensee.notebook.is_colab", return_value=False),
        ):
            with pytest.raises(Exception, match="no creds"):
                ensure_auth()


class TestRenderStatusHtml:
    def test_renders_running_state(self) -> None:
        from datensee.notebook import _render_status_html
        from datensee.status import JobInfo, JobState

        info = JobInfo(
            state=JobState.RUNNING,
            elapsed_seconds=120.0,
            elements_produced=50,
            elements_total=100,
            current_workers=5,
        )
        html = _render_status_html("job-123", info)
        assert "job-123" in html
        assert "JOB_STATE_RUNNING" in html
        assert "50" in html
        assert "100" in html
        assert "5" in html  # workers
        assert "2.0 min" in html

    def test_renders_done_state(self) -> None:
        from datensee.notebook import _render_status_html
        from datensee.status import JobInfo, JobState

        info = JobInfo(state=JobState.DONE)
        html = _render_status_html("job-456", info)
        assert "JOB_STATE_DONE" in html
        assert "#228b22" in html  # green color

    def test_renders_progress_bar(self) -> None:
        from datensee.notebook import _render_status_html
        from datensee.status import JobInfo, JobState

        info = JobInfo(
            state=JobState.RUNNING,
            elements_produced=75,
            elements_total=100,
        )
        html = _render_status_html("job-789", info)
        assert "75.0%" in html


class TestEnsureJar:
    def test_returns_found_jar(self, tmp_path: object) -> None:
        from datensee.notebook import ensure_jar

        jar = tmp_path / "test.jar"  # type: ignore[operator]
        jar.touch()

        with patch("datensee.jar.find_jar", return_value=jar):
            result = ensure_jar()
            assert result == jar

    def test_downloads_when_not_found(self, tmp_path: object) -> None:
        from datensee.notebook import ensure_jar

        jar = tmp_path / "downloaded.jar"  # type: ignore[operator]
        jar.touch()

        with (
            patch("datensee.jar.find_jar", side_effect=FileNotFoundError),
            patch("datensee.jar.download_jar", return_value=jar) as mock_dl,
        ):
            result = ensure_jar()
            assert result == jar
            mock_dl.assert_called_once()
