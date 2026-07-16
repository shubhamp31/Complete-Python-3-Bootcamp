"""Dynamic reader for Amazon flat-file (bulk listing) templates.

Amazon templates change layout between category versions, so nothing here
relies on fixed column numbers. Two template generations are supported:

* Classic flat files - one header row of machine names (``item_sku`` ...),
  located by scoring rows against known field names.
* New "Category Listings" templates - these embed their own layout in a
  hidden ``settings=`` cell on row 1 (``labelRow=4&attributeRow=5&dataRow=8``)
  and use attribute-path headers such as
  ``item_name[marketplace_id=...][language_tag=en_IN]#1.value``. The
  marketplace/language qualifiers are stripped to produce stable simplified
  aliases (``item_name#1.value``) that mappings can reference.

The workbook is always opened with ``keep_vba=True`` so macros, data
validation, drop-downs, hidden sheets and protection survive a round trip.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs

from openpyxl import load_workbook
from openpyxl.workbook.workbook import Workbook
from openpyxl.worksheet.worksheet import Worksheet

from core.utils import TemplateError, get_logger, normalise_header

logger = get_logger("amazon_template")

#: Bracket qualifiers that vary per marketplace/language and are dropped
#: when building simplified header aliases. Others (e.g. ``[audience=ALL]``)
#: are meaningful and kept.
_DROPPED_QUALIFIERS = re.compile(
    r"\[(?:marketplace_id|language_tag|content_language)=[^\]]*\]"
)


def simplify_attribute(header: str) -> str:
    """Strip marketplace/language qualifiers from an attribute-path header.

    ``item_name[marketplace_id=A21TJ...][language_tag=en_IN]#1.value``
    becomes ``item_name#1.value``.
    """
    return _DROPPED_QUALIFIERS.sub("", str(header)).strip()


@dataclass
class TemplateLayout:
    """Resolved structure of the Amazon template's data sheet."""

    sheet_name: str
    header_row: int
    data_start_row: int
    #: normalised header -> 1-based column index
    columns: dict[str, int] = field(default_factory=dict)
    #: normalised header -> original header text (for reporting)
    header_text: dict[str, str] = field(default_factory=dict)

    def column_for(self, field_name: str) -> int | None:
        """Return the 1-based column index for a field name, if present."""
        return self.columns.get(normalise_header(field_name))

    def has_field(self, field_name: str) -> bool:
        """Whether the template exposes the given field."""
        return normalise_header(field_name) in self.columns


class AmazonTemplate:
    """Loads an Amazon .xlsm/.xlsx template and resolves its layout."""

    def __init__(
        self,
        path: Path | str,
        sheet_name: str = "Template",
        header_hints: list[str] | None = None,
        header_search_max_rows: int = 10,
    ) -> None:
        self._path = Path(path)
        self._sheet_name = sheet_name
        self._header_hints = [
            normalise_header(h) for h in (header_hints or ["item_sku", "feed_product_type"])
        ]
        self._max_scan_rows = header_search_max_rows
        self._workbook: Workbook | None = None
        self._layout: TemplateLayout | None = None

    # ------------------------------------------------------------------ #

    @property
    def path(self) -> Path:
        """Path of the template workbook on disk."""
        return self._path

    @property
    def keep_vba(self) -> bool:
        """Whether the workbook must be saved with macros preserved."""
        return self._path.suffix.lower() == ".xlsm"

    def load(self) -> Workbook:
        """Open the workbook, preserving VBA when the file is an .xlsm.

        Raises:
            TemplateError: If the file cannot be opened as a workbook.
        """
        if self._workbook is not None:
            return self._workbook
        if not self._path.is_file():
            raise TemplateError(f"Amazon template not found: {self._path}")
        try:
            self._workbook = load_workbook(
                self._path, keep_vba=self.keep_vba, data_only=False
            )
        except Exception as exc:
            raise TemplateError(
                f"Could not open the Amazon template ({self._path.name}). "
                f"Is it a valid Excel workbook? Details: {exc}"
            ) from exc
        logger.info(
            "Loaded template %s (sheets: %s)",
            self._path.name,
            ", ".join(self._workbook.sheetnames),
        )
        return self._workbook

    def layout(self) -> TemplateLayout:
        """Resolve (and cache) sheet, header row and column positions.

        Raises:
            TemplateError: If no sheet contains a recognisable header row.
        """
        if self._layout is not None:
            return self._layout

        workbook = self.load()
        sheet = self._locate_sheet(workbook)
        embedded = self._read_embedded_settings(sheet)
        if embedded:
            header_row, data_start = embedded
            logger.info(
                "Template declares its own layout: attribute row=%d, data row=%d",
                header_row,
                data_start,
            )
        else:
            header_row = self._locate_header_row(sheet)
            data_start = header_row + 1

        columns: dict[str, int] = {}
        header_text: dict[str, str] = {}

        def register(key: str, column: int, original: str) -> None:
            key = normalise_header(key)
            if key and key not in columns:
                columns[key] = column
                header_text[key] = original

        for cell in sheet[header_row]:
            if cell.value is None or str(cell.value).strip() == "":
                continue
            original = str(cell.value).strip()
            register(original, cell.column, original)
            simplified = simplify_attribute(original)
            if simplified != original:
                register(simplified, cell.column, original)

        if not columns:
            raise TemplateError(
                f"No headers found on row {header_row} of sheet '{sheet.title}'."
            )

        self._layout = TemplateLayout(
            sheet_name=sheet.title,
            header_row=header_row,
            data_start_row=data_start,
            columns=columns,
            header_text=header_text,
        )
        logger.info(
            "Template layout: sheet='%s', header row=%d, %d columns",
            sheet.title,
            header_row,
            len(columns),
        )
        return self._layout

    def worksheet(self) -> Worksheet:
        """Return the resolved data worksheet."""
        return self.load()[self.layout().sheet_name]

    # ------------------------------------------------------------------ #

    def supported_product_types(self) -> list[str]:
        """Product types the template was generated for (new format only).

        New-style templates embed ``ptds=<base64 "EARRING,RING">`` in their
        settings row. Returns an empty list when the template does not
        declare them (classic flat files).
        """
        sheet = self.worksheet()
        for row in sheet.iter_rows(min_row=1, max_row=2, values_only=True):
            for value in row:
                if not isinstance(value, str) or "ptds=" not in value:
                    continue
                params = parse_qs(value.split("settings=", 1)[-1])
                try:
                    decoded = base64.b64decode(params["ptds"][0]).decode("utf-8")
                except (KeyError, ValueError, IndexError):
                    return []
                return [p.strip().upper() for p in decoded.split(",") if p.strip()]
        return []

    @staticmethod
    def _read_embedded_settings(sheet: Worksheet) -> tuple[int, int] | None:
        """Parse the ``settings=...`` cell new-style templates embed on row 1.

        Returns (attribute/header row, data start row) or ``None`` when the
        template is a classic flat file without embedded settings.
        """
        for row in sheet.iter_rows(min_row=1, max_row=2, values_only=True):
            for value in row:
                if not isinstance(value, str) or "attributeRow=" not in value:
                    continue
                query = value.split("settings=", 1)[-1]
                params = parse_qs(query)
                try:
                    attribute_row = int(params["attributeRow"][0])
                    data_row = int(params["dataRow"][0])
                except (KeyError, ValueError, IndexError):
                    logger.warning("Could not parse embedded template settings")
                    return None
                return attribute_row, data_row
        return None

    def _locate_sheet(self, workbook: Workbook) -> Worksheet:
        """Find the data-entry sheet, preferring the configured name."""
        for name in workbook.sheetnames:
            if name.strip().lower() == self._sheet_name.strip().lower():
                return workbook[name]
        # Fall back to the first sheet that contains a header hint.
        for name in workbook.sheetnames:
            sheet = workbook[name]
            if self._find_header_row(sheet) is not None:
                logger.warning(
                    "Sheet '%s' not found; using '%s' instead", self._sheet_name, name
                )
                return sheet
        raise TemplateError(
            f"Could not find a data sheet named '{self._sheet_name}' (or any sheet "
            "containing known Amazon headers) in the template."
        )

    def _locate_header_row(self, sheet: Worksheet) -> int:
        row = self._find_header_row(sheet)
        if row is None:
            raise TemplateError(
                f"Could not locate the header row on sheet '{sheet.title}'. "
                f"Expected one of: {', '.join(self._header_hints)}"
            )
        return row

    def _find_header_row(self, sheet: Worksheet) -> int | None:
        """Scan the top rows and return the one matching the most hints.

        Amazon templates stack a version banner, a display-name row and the
        machine field-name row; display names can coincide with field
        aliases, so the single best-scoring row wins (ties go to the later
        row, which is the one the data sits under).
        """
        best_row: int | None = None
        best_score = 0
        for row_idx, row in enumerate(
            sheet.iter_rows(min_row=1, max_row=self._max_scan_rows, values_only=True),
            start=1,
        ):
            normalised = {normalise_header(v) for v in row if v is not None}
            score = sum(1 for hint in self._header_hints if hint in normalised)
            if score >= best_score and score > 0:
                best_row, best_score = row_idx, score
        return best_row
