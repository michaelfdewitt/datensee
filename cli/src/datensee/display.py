"""Rich display components for the CLI.

Encapsulates all Rich rendering — keeps main.py focused on orchestration.
"""

from __future__ import annotations

from rich.panel import Panel
from rich.table import Table

from datensee.config import PipelineConfig
from datensee.estimate import CostEstimate


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


def _format_eecu(low: float, typical: float, high: float) -> str:
    """Format EECU range as low – typical – high."""
    return f"{low:.0f} – {typical:.0f} – {high:.0f} s  (low/typ/high)"


def _format_usd(amount: float) -> str:
    """Format a USD amount."""
    if amount < 0.01:
        return "<$0.01"
    if amount < 1.0:
        return f"${amount:.3f}"
    return f"${amount:.2f}"


def render_export_summary(config: PipelineConfig, estimate: CostEstimate) -> Panel:
    """Render a pre-submission summary panel with cost estimate.

    Args:
        config: Pipeline configuration.
        estimate: Cost estimate from estimate_cost().

    Returns:
        Rich Panel ready for console.print().
    """
    grid = config.tile_grid

    table = Table(show_header=False, show_edge=False, pad_edge=False, box=None)
    table.add_column("label", style="bold", min_width=14)
    table.add_column("value")

    # Export info
    tile_px = grid.tile_size_pixels
    table.add_row("Tiles", f"{estimate.tile_count:,}  ({tile_px}×{tile_px} px)")
    table.add_row("Scale", f"{grid.scale_meters} m/px  ({grid.crs})")
    table.add_row("Output", config.output.output_path)

    if config.runner.mode == "dataflow" and config.runner.dataflow is not None:
        df = config.runner.dataflow
        table.add_row(
            "Runner",
            f"dataflow  ({df.machine_type} × {df.max_workers} max)",
        )
    else:
        table.add_row("Runner", "local  (DirectRunner)")

    table.add_row("Rate limit", f"{config.rate_limit.max_qps} QPS")
    table.add_row("", "")  # spacer

    # Cost estimates
    table.add_row("Wall time", _format_duration(estimate.estimated_wall_seconds))
    table.add_row(
        "EECUs",
        _format_eecu(
            estimate.eecu_seconds_low,
            estimate.eecu_seconds_typical,
            estimate.eecu_seconds_high,
        ),
    )

    if estimate.dataflow_cost_usd is not None:
        table.add_row("Dataflow", _format_usd(estimate.dataflow_cost_usd))

    size = _format_bytes(estimate.output_size_bytes)
    cost = _format_usd(estimate.storage_cost_usd_per_month)
    table.add_row("Storage", f"{size}  ({cost}/mo)")

    return Panel(table, title="Export Summary", border_style="cyan")


def render_post_run_summary(
    duration_seconds: float,
    tiles_ok: int,
    tiles_failed: int,
    output_path: str,
) -> Panel:
    """Render a post-run summary panel.

    Args:
        duration_seconds: Total wall-clock duration.
        tiles_ok: Number of tiles successfully fetched.
        tiles_failed: Number of tiles that failed.
        output_path: Where the output was written.

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
    table.add_row("Output", output_path)

    style = "green" if tiles_failed == 0 else "yellow"
    title = "Complete" if tiles_failed == 0 else "Complete (with failures)"

    return Panel(table, title=title, border_style=style)
