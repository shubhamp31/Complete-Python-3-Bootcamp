"""Pydantic models shared across the application.

These models validate configuration and carry structured results between
pipeline stages, the validator and the UI.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class SourceType(str, Enum):
    """Where a mapped Amazon field gets its value from."""

    SHOPIFY = "shopify"     # a raw Shopify export column
    INTERNAL = "internal"   # a computed pipeline column (prefixed with "_")
    CONSTANT = "constant"   # a literal value from the mapping rule
    DEFAULT = "default"     # a key in defaults.json
    TEMPLATE = "template"   # a str.format template over working columns


class MappingSource(BaseModel):
    """Definition of a single mapping rule's data source."""

    type: SourceType
    column: str | None = None
    value: str | None = None
    key: str | None = None
    template: str | None = None

    @field_validator("type", mode="before")
    @classmethod
    def _coerce_type(cls, v: object) -> object:
        return str(v).lower() if isinstance(v, str) else v


class MappingRule(BaseModel):
    """One Amazon field -> source mapping, loaded from field_mapping.json."""

    amazon_field: str
    aliases: list[str] = Field(default_factory=list)
    source: MappingSource

    def all_names(self) -> list[str]:
        """Return the canonical field name plus all aliases."""
        return [self.amazon_field, *self.aliases]


class FieldMappingConfig(BaseModel):
    """Full parsed field_mapping.json."""

    marketplace: str = "amazon_in"
    template_sheet: str = "Template"
    header_row_hints: list[str] = Field(default_factory=lambda: ["item_sku"])
    header_search_max_rows: int = 10
    mappings: list[MappingRule]


class Severity(str, Enum):
    """Validation issue severity."""

    ERROR = "Error"
    WARNING = "Warning"


class ValidationIssue(BaseModel):
    """A single validation finding tied to an output row."""

    row: int = Field(description="1-based row number in the generated listing data")
    sku: str = ""
    field: str = ""
    severity: Severity = Severity.ERROR
    message: str


class GenerationRequest(BaseModel):
    """User inputs required to run the pipeline."""

    shopify_csv: Path
    amazon_template: Path
    output_dir: Path

    @field_validator("shopify_csv")
    @classmethod
    def _csv_exists(cls, v: Path) -> Path:
        if not v.is_file():
            raise ValueError(f"Shopify CSV not found: {v}")
        return v

    @field_validator("amazon_template")
    @classmethod
    def _template_exists(cls, v: Path) -> Path:
        if not v.is_file():
            raise ValueError(f"Amazon template not found: {v}")
        if v.suffix.lower() not in {".xlsm", ".xlsx"}:
            raise ValueError("Amazon template must be an .xlsm or .xlsx workbook")
        return v


class SummaryStats(BaseModel):
    """Aggregate numbers for the summary report and success dialog."""

    total_shopify_rows: int = 0
    total_products: int = 0
    total_listings: int = 0
    parents: int = 0
    children: int = 0
    standalone: int = 0
    missing_images: int = 0
    errors: int = 0
    warnings: int = 0
    successful_listings: int = 0
    elapsed_seconds: float = 0.0


class GenerationResult(BaseModel):
    """Everything the UI needs after a pipeline run."""

    status: Literal["success", "completed_with_errors", "failed"]
    output_file: Path | None = None
    validation_report: Path | None = None
    error_report: Path | None = None
    summary_report: Path | None = None
    stats: SummaryStats = Field(default_factory=SummaryStats)
    message: str = ""
