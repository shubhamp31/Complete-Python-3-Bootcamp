"""Excel output: populates the Amazon template and writes report workbooks.

The Amazon template is opened via :class:`core.amazon_template.AmazonTemplate`
with ``keep_vba=True``, and this writer only ever sets cell *values* below
the header row. Formatting, data validation, drop-downs, macros, hidden
sheets and protection are never touched, so they survive intact.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from core.amazon_template import AmazonTemplate
from core.models import MappingRule, Severity, SummaryStats
from core.utils import get_logger

logger = get_logger("excel_writer")

_HEADER_FILL = PatternFill("solid", fgColor="1F4E79")
_HEADER_FONT = Font(bold=True, color="FFFFFF")
_ERROR_FILL = PatternFill("solid", fgColor="FFC7CE")
_WARNING_FILL = PatternFill("solid", fgColor="FFEB9C")


class ExcelWriter:
    """Writes the upload file and all report workbooks."""

    # ------------------------------------------------------------------ #
    # Amazon upload file
    # ------------------------------------------------------------------ #

    def write_upload_file(
        self,
        template: AmazonTemplate,
        amazon: pd.DataFrame,
        rules: Iterable[MappingRule],
        output_path: Path,
    ) -> tuple[Path, list[str]]:
        """Populate the template with listing data and save it.

        Args:
            template: Loaded Amazon template (macros preserved).
            amazon: Mapped listing table (canonical Amazon field columns).
            rules: Mapping rules, used to match canonical names and their
                aliases against the template's actual headers.
            output_path: Destination file (extension is forced to match
                the template so macros stay valid).

        Returns:
            Tuple of (written path, list of fields skipped because the
            template has no matching column).
        """
        layout = template.layout()
        sheet = template.worksheet()

        column_of: dict[str, int] = {}
        skipped: list[str] = []
        for rule in rules:
            col = next(
                (c for name in rule.all_names() if (c := layout.column_for(name))),
                None,
            )
            if col is None:
                skipped.append(rule.amazon_field)
            elif rule.amazon_field in amazon.columns:
                column_of[rule.amazon_field] = col

        if skipped:
            logger.warning(
                "Template has no column for %d mapped field(s): %s",
                len(skipped),
                ", ".join(skipped),
            )
        if not column_of:
            raise ValueError(
                "None of the mapped fields exist in the Amazon template; "
                "check field_mapping.json against the template headers."
            )

        start = layout.data_start_row
        for offset, (_, record) in enumerate(amazon.iterrows()):
            row_idx = start + offset
            for field, col_idx in column_of.items():
                value = record[field]
                if value is None or str(value) == "":
                    continue
                sheet.cell(row=row_idx, column=col_idx, value=value)

        # Keep the template's own extension: .xlsm output for macro-enabled
        # templates, plain .xlsx otherwise.
        expected_suffix = ".xlsm" if template.keep_vba else ".xlsx"
        if output_path.suffix.lower() != expected_suffix:
            output_path = output_path.with_suffix(expected_suffix)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        template.load().save(output_path)
        logger.info(
            "Wrote %d listing rows x %d columns to %s",
            len(amazon),
            len(column_of),
            output_path,
        )
        return output_path, skipped

    # ------------------------------------------------------------------ #
    # Reports
    # ------------------------------------------------------------------ #

    def write_issue_report(
        self,
        issues: pd.DataFrame,
        path: Path,
        title: str,
        errors_only: bool = False,
    ) -> Path:
        """Write the validation or error report with severity highlighting."""
        frame = issues.copy()
        if errors_only:
            frame = frame[frame["Severity"] == Severity.ERROR.value]

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = title[:31]
        self._write_table(sheet, frame)

        severity_col = (
            list(frame.columns).index("Severity") + 1 if "Severity" in frame.columns else 0
        )
        if severity_col:
            for row_idx in range(2, len(frame) + 2):
                severity = sheet.cell(row=row_idx, column=severity_col).value
                fill = (
                    _ERROR_FILL
                    if severity == Severity.ERROR.value
                    else _WARNING_FILL
                    if severity == Severity.WARNING.value
                    else None
                )
                if fill:
                    for col_idx in range(1, len(frame.columns) + 1):
                        sheet.cell(row=row_idx, column=col_idx).fill = fill

        path.parent.mkdir(parents=True, exist_ok=True)
        workbook.save(path)
        logger.info("Wrote %s (%d rows) to %s", title, len(frame), path)
        return path

    def write_summary_report(
        self,
        stats: SummaryStats,
        path: Path,
        extra: dict[str, Any] | None = None,
    ) -> Path:
        """Write the run summary workbook."""
        rows = [
            ("Total Shopify Rows", stats.total_shopify_rows),
            ("Total Products", stats.total_products),
            ("Total Listings Generated", stats.total_listings),
            ("Parent Listings", stats.parents),
            ("Child Listings", stats.children),
            ("Standalone Listings", stats.standalone),
            ("Listings Missing Images", stats.missing_images),
            ("Validation Errors", stats.errors),
            ("Validation Warnings", stats.warnings),
            ("Successful Listings", stats.successful_listings),
            ("Processing Time (seconds)", round(stats.elapsed_seconds, 2)),
        ]
        for key, value in (extra or {}).items():
            rows.append((key, value))

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Summary"
        frame = pd.DataFrame(rows, columns=["Metric", "Value"])
        self._write_table(sheet, frame)

        path.parent.mkdir(parents=True, exist_ok=True)
        workbook.save(path)
        logger.info("Wrote summary report to %s", path)
        return path

    # ------------------------------------------------------------------ #

    @staticmethod
    def _write_table(sheet: Any, frame: pd.DataFrame) -> None:
        """Write a DataFrame with a styled header row and sized columns."""
        for col_idx, name in enumerate(frame.columns, start=1):
            cell = sheet.cell(row=1, column=col_idx, value=str(name))
            cell.fill = _HEADER_FILL
            cell.font = _HEADER_FONT
            cell.alignment = Alignment(horizontal="center")

        for row_offset, record in enumerate(frame.itertuples(index=False), start=2):
            for col_idx, value in enumerate(record, start=1):
                sheet.cell(row=row_offset, column=col_idx, value=value)

        for col_idx, name in enumerate(frame.columns, start=1):
            sample = frame[name].astype(str).str.len().max() if len(frame) else 0
            width = min(max(int(sample or 0), len(str(name))) + 3, 80)
            sheet.column_dimensions[get_column_letter(col_idx)].width = width
        sheet.freeze_panes = "A2"
