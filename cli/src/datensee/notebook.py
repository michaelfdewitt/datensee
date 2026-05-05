"""Jupyter/Colab environment detection, auth helpers, and display adapters.

All notebook-specific logic lives here. Functions are safe to call outside
notebooks — they either no-op or fall back to non-notebook behavior.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datensee.config import PipelineConfig, TileGrid
    from datensee.status import JobInfo, JobState


# ---------------------------------------------------------------------------
# Environment detection
# ---------------------------------------------------------------------------


def is_notebook() -> bool:
    """Detect whether code is running inside a Jupyter/Colab kernel."""
    try:
        from IPython import get_ipython

        shell = get_ipython()
        return shell is not None and "IPKernelApp" in shell.config
    except (ImportError, AttributeError):
        return False


def is_colab() -> bool:
    """Detect whether code is running inside Google Colab."""
    return "google.colab" in sys.modules


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def ensure_auth() -> None:
    """Ensure GCP credentials are available, triggering Colab auth if needed.

    In Colab: always calls ``google.colab.auth.authenticate_user()`` (idempotent
    — won't re-prompt if already authed), then exports user credentials to the
    standard ADC file so that Java subprocesses can discover them.

    Outside Colab: no-ops if ADC is available, raises if not.
    """
    if is_colab():
        from google.colab import auth  # type: ignore[import-untyped]

        auth.authenticate_user()
        _export_adc_for_java()
        return

    import google.auth

    google.auth.default()


def _export_adc_for_java() -> None:
    """Write Python credentials to the standard ADC file for Java subprocesses.

    Colab's ``authenticate_user()`` makes credentials available to Python but
    not to the well-known file that Java's ``GoogleCredentials.getApplicationDefault()``
    checks. This bridges the gap by serializing the user credentials to
    ``~/.config/gcloud/application_default_credentials.json``.

    Best-effort — silently skips if credentials lack the required fields
    (e.g. GCE compute credentials instead of user OAuth credentials).
    """
    import json as _json

    import google.auth

    adc_path = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
    if adc_path.exists():
        return

    try:
        creds, _ = google.auth.default()
    except Exception:
        return

    # Only user credentials (from the Colab OAuth flow) have these fields.
    # GCE compute credentials don't — skip without error.
    client_id = getattr(creds, "client_id", None)
    client_secret = getattr(creds, "client_secret", None)
    refresh_token = getattr(creds, "refresh_token", None)

    if not all([client_id, client_secret, refresh_token]):
        return

    adc = {
        "type": "authorized_user",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    }

    adc_path.parent.mkdir(parents=True, exist_ok=True)
    adc_path.write_text(_json.dumps(adc))


# ---------------------------------------------------------------------------
# JAR resolution (local mode only)
# ---------------------------------------------------------------------------


def ensure_jar() -> Path:
    """Locate the pipeline JAR for local-mode submission.

    Cloud submission (Dataflow Flex Template) does not need a local JAR
    and never calls this. Raises :class:`FileNotFoundError` with build
    instructions when no JAR is on disk.
    """
    from datensee.jar import find_jar

    return find_jar(None)


# ---------------------------------------------------------------------------
# Display adapters (HTML for notebook cells)
# ---------------------------------------------------------------------------

_STATUS_HTML_TEMPLATE = """\
<div style="font-family: monospace; padding: 8px; border: 1px solid #ddd; border-radius: 4px;">
  <table style="border-collapse: collapse; width: 100%%;">
    <tr><td style="padding: 4px 8px; font-weight: bold;">Job</td>
        <td style="padding: 4px 8px;">{job_id}</td></tr>
    <tr><td style="padding: 4px 8px; font-weight: bold;">State</td>
        <td style="padding: 4px 8px; color: {state_color};">{state}</td></tr>
    {elapsed_row}
    {workers_row}
    {tiles_row}
  </table>
  {progress_bar}
</div>
"""

_STATE_COLORS = {
    "JOB_STATE_PENDING": "#b8860b",
    "JOB_STATE_RUNNING": "#0077cc",
    "JOB_STATE_DONE": "#228b22",
    "JOB_STATE_FAILED": "#cc0000",
    "JOB_STATE_CANCELLED": "#8b008b",
    "JOB_STATE_UNKNOWN": "#888",
}


def _render_status_html(job_id: str, info: JobInfo) -> str:
    """Render job status as an HTML string for notebook display."""
    state_str = info.state.value
    color = _STATE_COLORS.get(state_str, "#888")

    elapsed_row = ""
    if info.elapsed_seconds is not None:
        minutes = info.elapsed_seconds / 60
        elapsed_row = (
            f'<tr><td style="padding: 4px 8px; font-weight: bold;">Elapsed</td>'
            f'<td style="padding: 4px 8px;">{minutes:.1f} min</td></tr>'
        )

    workers_row = ""
    if info.current_workers is not None:
        workers_row = (
            f'<tr><td style="padding: 4px 8px; font-weight: bold;">Workers</td>'
            f'<td style="padding: 4px 8px;">{info.current_workers}</td></tr>'
        )

    tiles_row = ""
    progress_bar = ""
    if info.elements_produced is not None:
        total_str = f" / {info.elements_total}" if info.elements_total else ""
        tiles_row = (
            f'<tr><td style="padding: 4px 8px; font-weight: bold;">Tiles</td>'
            f'<td style="padding: 4px 8px;">{info.elements_produced}{total_str}'
            f' <span style="color: #888;">(~30s lag)</span></td></tr>'
        )
        if info.elements_total and info.elements_total > 0:
            pct = min(100, info.elements_produced / info.elements_total * 100)
            progress_bar = (
                f'<div style="margin-top: 8px; background: #eee; '
                f'border-radius: 4px; height: 20px; overflow: hidden;">'
                f'<div style="background: {color}; height: 100%; '
                f'width: {pct:.1f}%; transition: width 0.3s;"></div></div>'
            )

    return _STATUS_HTML_TEMPLATE.format(
        job_id=job_id,
        state=state_str,
        state_color=color,
        elapsed_row=elapsed_row,
        workers_row=workers_row,
        tiles_row=tiles_row,
        progress_bar=progress_bar,
    )


def display_job_progress(
    job_id: str,
    project: str,
    region: str = "us-central1",
    *,
    poll_interval: int = 15,
) -> JobState:
    """Poll a Dataflow job with HTML status updates in a notebook cell.

    Each poll tick clears the cell output and renders an HTML status table.
    This works reliably in both Colab and JupyterLab.

    Args:
        job_id: Dataflow job ID.
        project: GCP project ID.
        region: Dataflow region.
        poll_interval: Seconds between polls.

    Returns:
        Final JobState.
    """
    from IPython.display import HTML, clear_output, display

    from datensee.api import poll

    def on_status(info: JobInfo) -> None:
        clear_output(wait=True)
        display(HTML(_render_status_html(job_id, info)))

    return poll(
        job_id,
        project,
        region,
        callback=on_status,
        poll_interval=poll_interval,
    )


def display_export_summary(config: PipelineConfig) -> None:
    """Render an export summary as an HTML table in a notebook cell.

    Args:
        config: Pipeline configuration.
    """
    from IPython.display import HTML, display

    from datensee.display import _format_bytes

    grid = config.tile_grid
    tile_px = grid.tile_size_pixels

    rows = [
        ("Tiles", f"{config.tile_count:,} ({tile_px}&times;{tile_px} px)"),
        ("Pixel size", f"{grid.pixel_size:g} {grid.crs} units"),
        ("Output", config.output.output_path),
        ("Raw size", _format_bytes(config.raw_output_bytes)),
    ]

    if config.runner.mode == "dataflow" and config.runner.dataflow is not None:
        df = config.runner.dataflow
        rows.append(("Runner", f"dataflow ({df.machine_type} &times; {df.max_workers} max)"))
    else:
        rows.append(("Runner", "local (DirectRunner)"))

    rows.append(("Rate limit", f"{config.rate_limit.max_qps} QPS"))

    row_html = "\n".join(
        f'<tr><td style="padding: 4px 12px; font-weight: bold;">{label}</td>'
        f'<td style="padding: 4px 12px;">{value}</td></tr>'
        for label, value in rows
    )

    html = (
        f'<div style="font-family: monospace; padding: 8px; border: 1px solid #0077cc; '
        f'border-radius: 4px;">'
        f'<div style="font-weight: bold; padding: 4px 12px; color: #0077cc; '
        f'margin-bottom: 4px;">Export Summary</div>'
        f'<table style="border-collapse: collapse; width: 100%;">{row_html}</table>'
        f"</div>"
    )
    display(HTML(html))


def display_tile_grid(grid: TileGrid, region: dict[str, Any]) -> None:
    """Show tile outlines and region polygon with matplotlib.

    Args:
        grid: TileGrid from decompose_region() or api.tile().
        region: Original GeoJSON region geometry dict.
    """
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle
    from shapely.geometry import shape

    from datensee.tiling import tile_bbox

    fig, ax = plt.subplots(1, 1, figsize=(10, 8))

    # Draw tiles
    if grid.tiles:
        for t in grid.tiles:
            x_min, y_min, x_max, y_max = tile_bbox(grid.pixel_grid, t)
            rect = Rectangle(
                (x_min, y_min),
                x_max - x_min,
                y_max - y_min,
                linewidth=0.5,
                edgecolor="#0077cc",
                facecolor="#0077cc",
                alpha=0.1,
            )
            ax.add_patch(rect)

    # Draw region outline
    geom = shape(region)
    if hasattr(geom, "exterior"):
        xs, ys = geom.exterior.xy
        ax.plot(xs, ys, color="#cc0000", linewidth=2, label="Region")
    elif hasattr(geom, "geoms"):
        for i, poly in enumerate(geom.geoms):
            xs, ys = poly.exterior.xy
            ax.plot(xs, ys, color="#cc0000", linewidth=2, label="Region" if i == 0 else None)

    ax.set_xlabel(f"X ({grid.crs})")
    ax.set_ylabel(f"Y ({grid.crs})")
    ax.set_title(f"{len(grid.tiles or [])} tiles @ {grid.pixel_size:g} {grid.crs} units/px")
    ax.legend()
    ax.set_aspect("equal")
    ax.autoscale()
    plt.tight_layout()
    plt.show()


def preview_tiles(
    output: str,
    config: PipelineConfig,
    n: int = 4,
) -> None:
    """Download and display N sample tiles as matplotlib images.

    For GCS paths, downloads tile bytes via google-cloud-storage. For local
    paths, reads directly. Requires rasterio (datensee[validation] extra).

    Args:
        output: Output path (GCS URI or local directory).
        config: Pipeline config that produced the output.
        n: Number of tiles to preview.
    """
    import matplotlib.pyplot as plt
    import rasterio

    tiles = config.tile_grid.tiles
    if not tiles:
        return

    # Sample tiles (evenly spaced)
    step = max(1, len(tiles) // n)
    sample = tiles[::step][:n]

    fig, axes = plt.subplots(1, len(sample), figsize=(4 * len(sample), 4))
    if len(sample) == 1:
        axes = [axes]

    for ax, t in zip(axes, sample, strict=True):
        tile_name = f"tile_r{t.row:04d}_c{t.col:04d}.tif"

        if output.startswith("gs://"):
            from google.cloud import storage

            parts = output.replace("gs://", "").split("/", 1)
            bucket_name = parts[0]
            prefix = parts[1] if len(parts) > 1 else ""
            blob_path = f"{prefix.rstrip('/')}/{tile_name}" if prefix else tile_name

            client = storage.Client()
            bucket = client.bucket(bucket_name)
            blob = bucket.blob(blob_path)
            data = blob.download_as_bytes()

            import tempfile

            with tempfile.NamedTemporaryFile(suffix=".tif") as tmp:
                tmp.write(data)
                tmp.flush()
                with rasterio.open(tmp.name) as src:
                    band = src.read(1)
        else:
            tile_path = Path(output) / tile_name
            if not tile_path.exists():
                ax.set_title(f"{tile_name}\n(missing)")
                ax.axis("off")
                continue
            with rasterio.open(tile_path) as src:
                band = src.read(1)

        ax.imshow(band, cmap="viridis")
        ax.set_title(f"r{t.row} c{t.col}")
        ax.axis("off")

    plt.suptitle("Tile Preview")
    plt.tight_layout()
    plt.show()
