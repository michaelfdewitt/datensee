"""Rich display components for the CLI.

Encapsulates Rich terminal rendering for the CLI.
"""

from __future__ import annotations

from rich.panel import Panel
from rich.table import Table

from datensee.config import PipelineConfig
from datensee.cost import CostEstimate, estimate_cost


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


def _format_usd(value: float) -> str:
    """Format a USD amount, flooring tiny non-zero values at '<$0.01'."""
    if 0 < value < 0.005:
        return "<$0.01"
    return f"${value:,.2f}"


def _add_cost_rows(table: Table, estimate: CostEstimate) -> None:
    """Append the 'Estimated cost' rows to an export-summary table."""
    if estimate.tile_count == 0:
        table.add_row(
            "Est. cost", "[dim]unavailable: tile count unknown (tiles externalized)[/dim]"
        )
        return

    table.add_row(
        "Est. EECU",
        f"{estimate.eecu_seconds_low:,.0f}–{estimate.eecu_seconds_high:,.0f} EECU-s "
        f"({estimate.eecu_hours_low:,.2f}–{estimate.eecu_hours_high:,.2f} EECU-h)  "
        "[dim]free for non-commercial EE use[/dim]",
    )
    if estimate.dataflow_usd_low is None or estimate.dataflow_usd_high is None:
        table.add_row("Est. Dataflow", "none (local runner)")
    else:
        table.add_row(
            "Est. Dataflow",
            f"{_format_usd(estimate.dataflow_usd_low)}–{_format_usd(estimate.dataflow_usd_high)}",
        )
    if estimate.shuffle_usd is not None:
        table.add_row("Est. shuffle", f"{_format_usd(estimate.shuffle_usd)}  (two-tier GroupByKey)")
    table.add_row("Est. storage", f"{_format_usd(estimate.storage_usd_per_month)}/month")
    table.add_row("", "[dim]rough estimate; EECU usage is expression-dependent[/dim]")


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

    _add_cost_rows(table, estimate_cost(config))

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
