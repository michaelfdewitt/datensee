"""Eval result models and Rich rendering.

EvalResult captures per-eval pass/fail/skip with details.
EvalReport aggregates results and renders a summary table.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel
from rich.panel import Panel
from rich.table import Table

from datensee.config import PipelineConfig
from datensee.eval.catalog import EvalID, get_eval


class EvalStatus(StrEnum):
    """Outcome of a single eval."""

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"


class EvalResult(BaseModel):
    """Result of running one eval."""

    eval_id: EvalID
    status: EvalStatus
    message: str = ""
    details: dict[str, Any] = {}


class EvalReport(BaseModel):
    """Aggregated results from validate_output()."""

    results: list[EvalResult]
    output_path: str
    config: PipelineConfig

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.status == EvalStatus.PASSED)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status == EvalStatus.FAILED)

    @property
    def all_passed(self) -> bool:
        return all(r.status in (EvalStatus.PASSED, EvalStatus.SKIPPED) for r in self.results)

    def to_dict(self) -> dict[str, Any]:
        """Serialize report to a JSON-compatible dict."""
        return {
            "output_path": self.output_path,
            "summary": {
                "passed": self.passed,
                "failed": self.failed,
                "total": len(self.results),
            },
            "results": [
                {
                    "eval_id": r.eval_id.value,
                    "status": r.status.value,
                    "message": r.message,
                    "details": r.details,
                }
                for r in self.results
            ],
        }

    def render(self) -> Panel:
        """Render a Rich panel summarizing eval results."""
        table = Table(show_edge=False, pad_edge=False)
        table.add_column("Eval", style="bold", min_width=6)
        table.add_column("Name", min_width=24)
        table.add_column("Status", min_width=8)
        table.add_column("Message")

        _status_style = {
            EvalStatus.PASSED: "[green]PASS[/green]",
            EvalStatus.FAILED: "[red]FAIL[/red]",
            EvalStatus.SKIPPED: "[dim]SKIP[/dim]",
            EvalStatus.ERROR: "[yellow]ERR[/yellow]",
        }

        for result in self.results:
            defn = get_eval(result.eval_id)
            table.add_row(
                result.eval_id.value,
                defn.name,
                _status_style[result.status],
                result.message,
            )

        n_total = len(self.results)
        style = "green" if self.all_passed else "red"
        title = f"Eval Results — {self.passed}/{n_total} passed"

        return Panel(table, title=title, border_style=style)
