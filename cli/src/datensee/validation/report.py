"""Validation result models and Rich rendering.

CheckResult captures per-check pass/fail/skip with details.
ValidationReport aggregates results and renders a summary table.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel
from rich.panel import Panel
from rich.table import Table

from datensee.config import PipelineConfig
from datensee.validation.catalog import CheckID, get_check


class CheckStatus(StrEnum):
    """Outcome of a single check."""

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"


class CheckResult(BaseModel):
    """Result of running one check."""

    check_id: CheckID
    status: CheckStatus
    message: str = ""
    details: dict[str, Any] = {}


class ValidationReport(BaseModel):
    """Aggregated results from validate_output()."""

    results: list[CheckResult]
    output_path: str
    config: PipelineConfig

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.status == CheckStatus.PASSED)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status == CheckStatus.FAILED)

    @property
    def all_passed(self) -> bool:
        return all(r.status in (CheckStatus.PASSED, CheckStatus.SKIPPED) for r in self.results)

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
                    "check_id": r.check_id.value,
                    "status": r.status.value,
                    "message": r.message,
                    "details": r.details,
                }
                for r in self.results
            ],
        }

    def render(self) -> Panel:
        """Render a Rich panel summarizing check results."""
        table = Table(show_edge=False, pad_edge=False)
        table.add_column("Check", style="bold", min_width=6)
        table.add_column("Name", min_width=24)
        table.add_column("Status", min_width=8)
        table.add_column("Message")

        _status_style = {
            CheckStatus.PASSED: "[green]PASS[/green]",
            CheckStatus.FAILED: "[red]FAIL[/red]",
            CheckStatus.SKIPPED: "[dim]SKIP[/dim]",
            CheckStatus.ERROR: "[yellow]ERR[/yellow]",
        }

        for result in self.results:
            defn = get_check(result.check_id)
            table.add_row(
                result.check_id.value,
                defn.name,
                _status_style[result.status],
                result.message,
            )

        n_total = len(self.results)
        style = "green" if self.all_passed else "red"
        title = f"Validation results — {self.passed}/{n_total} passed"

        return Panel(table, title=title, border_style=style)
