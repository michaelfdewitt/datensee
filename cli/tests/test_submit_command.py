"""Tests for `_build_command` — the Dataflow/Direct runner argv builder."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

from datensee.config import (
    DataflowRunnerConfig,
    OutputConfig,
    PipelineConfig,
    RunnerConfig,
    TileCoordinate,
    TileGrid,
)
from datensee.submit import _build_command, _prepare_user_token_fd


def _config(runner: RunnerConfig) -> PipelineConfig:
    return PipelineConfig(
        ee_expression='{"result":"0","values":{}}',
        gee_project="my-gcp-project",
        tile_grid=TileGrid(
            crs="EPSG:4326",
            scale_meters=30.0,
            tiles=[TileCoordinate(x_min=0, y_min=0, x_max=1, y_max=1, row=0, col=0)],
        ),
        output=OutputConfig(output_path="gs://my-bucket/exports/test"),
        runner=runner,
    )


def test_local_runner_command_has_direct_runner() -> None:
    cmd = _build_command(
        _config(RunnerConfig(mode="local")),
        jar_path=Path("/tmp/fake.jar"),
        config_path=Path("/tmp/cfg.json"),
    )
    assert "--runner=DirectRunner" in cmd
    assert not any(c.startswith("--labels") for c in cmd)


def test_dataflow_command_omits_labels_when_none() -> None:
    cmd = _build_command(
        _config(
            RunnerConfig(
                mode="dataflow",
                dataflow=DataflowRunnerConfig(
                    project="p",
                    region="us-central1",
                    temp_location="gs://b/tmp",
                    staging_location="gs://b/staging",
                ),
            )
        ),
        jar_path=Path("/tmp/fake.jar"),
        config_path=Path("/tmp/cfg.json"),
    )
    assert "--runner=DataflowRunner" in cmd
    assert not any(c.startswith("--labels") for c in cmd)


def test_dataflow_command_includes_labels_as_json() -> None:
    cmd = _build_command(
        _config(
            RunnerConfig(
                mode="dataflow",
                dataflow=DataflowRunnerConfig(
                    project="p",
                    region="us-central1",
                    temp_location="gs://b/tmp",
                    staging_location="gs://b/staging",
                    labels={"foundree": "1"},
                ),
            )
        ),
        jar_path=Path("/tmp/fake.jar"),
        config_path=Path("/tmp/cfg.json"),
    )
    label_flags = [c for c in cmd if c.startswith("--labels=")]
    assert len(label_flags) == 1
    payload = label_flags[0].removeprefix("--labels=")
    assert json.loads(payload) == {"foundree": "1"}


def test_dataflow_command_labels_multi_key() -> None:
    cmd = _build_command(
        _config(
            RunnerConfig(
                mode="dataflow",
                dataflow=DataflowRunnerConfig(
                    project="p",
                    region="us-central1",
                    temp_location="gs://b/tmp",
                    staging_location="gs://b/staging",
                    labels={"foundree": "1", "team": "geo"},
                ),
            )
        ),
        jar_path=Path("/tmp/fake.jar"),
        config_path=Path("/tmp/cfg.json"),
    )
    label_flags = [c for c in cmd if c.startswith("--labels=")]
    assert len(label_flags) == 1
    payload = label_flags[0].removeprefix("--labels=")
    assert json.loads(payload) == {"foundree": "1", "team": "geo"}


def test_prepare_user_token_fd_returns_none_without_credentials() -> None:
    assert _prepare_user_token_fd(None) is None


def test_prepare_user_token_fd_round_trip() -> None:
    """Verify the pipe FD carries exactly the token bytes and then EOFs.

    Simulates what the Java driver does on the other side: read from the
    inherited FD via `/proc/self/fd/<N>` (here we just read the FD directly
    since we're the same process) and confirm we see the token followed by
    EOF. Also verifies the FD is inheritable so subprocess.Popen(pass_fds=...)
    can actually hand it to the child.
    """
    creds = MagicMock()
    creds.token = "ya29.test-token-value"
    creds.expired = False

    fd = _prepare_user_token_fd(creds)
    assert fd is not None
    try:
        # subprocess.Popen(pass_fds=...) works because the FD is marked
        # inheritable. We verify that flag explicitly so a regression in
        # _prepare_user_token_fd can't silently break the child handoff.
        assert os.get_inheritable(fd)
        # Read all bytes — must see the token and then EOF immediately,
        # because the write end was closed inside the helper.
        data = os.read(fd, 4096)
        assert data == b"ya29.test-token-value"
        assert os.read(fd, 4096) == b""  # EOF
    finally:
        os.close(fd)


def test_prepare_user_token_fd_refreshes_expired() -> None:
    """An expired credential is refreshed once before the FD handoff."""
    creds = MagicMock()
    creds.token = None
    creds.expired = True

    def _do_refresh(_request: object) -> None:
        creds.token = "ya29.refreshed"
        creds.expired = False

    creds.refresh.side_effect = _do_refresh

    fd = _prepare_user_token_fd(creds)
    assert fd is not None
    try:
        assert os.read(fd, 4096) == b"ya29.refreshed"
    finally:
        os.close(fd)
    creds.refresh.assert_called_once()
