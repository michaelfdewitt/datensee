"""Rich display components for the CLI.

Encapsulates all Rich rendering — keeps main.py focused on orchestration.
"""

from __future__ import annotations

from rich.panel import Panel
from rich.table import Table

from datensee.config import PipelineConfig


def _format_duration(seconds: float) -> str:
    """Format seconds into a human-readable duration string."""
    if seconds < 60:
        return f"~{seconds:.0f} s"
    if seconds < 3600:
        minutes = seconds / 60
        return f"~{minutes:.1f} min"
    hours = seconds / 3600
    return f"~{hours:.1f} h"


def _format_bytes(n: int) -> str:
    """Format byte count into human-readable string."""
    if n < 1024:
        return f"{n} B"
    if n < 1024**2:
        return f"{n / 1024:.1f} KB"
    if n < 1024**3:
        return f"{n / 1024**2:.1f} MB"
    return f"{n / 1024**3:.2f} GB"


def render_export_summary(config: PipelineConfig) -> Panel:
    """Render a pre-submission summary panel with factual config info.

    Args:
        config: Pipeline configuration.

    Returns:
        Rich Panel ready for console.print().
    """
    grid = config.tile_grid

    table = Table(show_header=False, show_edge=False, pad_edge=False, box=None)
    table.add_column("label", style="bold", min_width=14)
    table.add_column("value")

    tile_px = grid.tile_size_pixels
    table.add_row("Tiles", f"{config.tile_count:,}  ({tile_px}×{tile_px} px)")
    table.add_row("Pixel size", f"{grid.pixel_size:g} {grid.crs} units")
    table.add_row("Output", config.output.output_path)
    table.add_row("Raw size", _format_bytes(config.raw_output_bytes))

    if config.runner.mode == "dataflow" and config.runner.dataflow is not None:
        df = config.runner.dataflow
        table.add_row(
            "Runner",
            f"dataflow  ({df.machine_type}, {df.num_workers}–{df.max_workers} workers, "
            f"{df.number_of_worker_harness_threads} threads/worker)",
        )
    else:
        table.add_row("Runner", "local  (DirectRunner)")

    return Panel(table, title="Export Summary", border_style="cyan")


def render_post_run_summary(
    duration_seconds: float,
    tiles_ok: int,
    tiles_failed: int,
    output_path: str,
    *,
    output_bytes: int | None = None,
) -> Panel:
    """Render a post-run summary panel.

    Args:
        duration_seconds: Total wall-clock duration.
        tiles_ok: Number of tiles successfully fetched.
        tiles_failed: Number of tiles that failed.
        output_path: Where the output was written.
        output_bytes: Actual output size in bytes (if measured).

    Returns:
        Rich Panel ready for console.print().
    """
    table = Table(show_header=False, show_edge=False, pad_edge=False, box=None)
    table.add_column("label", style="bold", min_width=14)
    table.add_column("value")

    table.add_row("Duration", _format_duration(duration_seconds))
    table.add_row("Tiles OK", f"{tiles_ok:,}")
    if tiles_failed > 0:
        table.add_row("Tiles failed", f"[red]{tiles_failed:,}[/red]")
    if output_bytes is not None:
        table.add_row("Output size", _format_bytes(output_bytes))
    table.add_row("Output", output_path)

    style = "green" if tiles_failed == 0 else "yellow"
    title = "Complete" if tiles_failed == 0 else "Complete (with failures)"

    return Panel(table, title=title, border_style=style)
