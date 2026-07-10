"""End-to-end Amazon India generation pipeline.

Orchestrates: read Shopify export -> build variations -> enrich business
fields -> map images -> generate SEO -> map to Amazon fields -> validate ->
write the upload file and reports. Registered as the ``amazon_in``
marketplace generator.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from core.amazon_template import AmazonTemplate
from core.excel_writer import ExcelWriter
from core.image_mapper import ImageMapper
from core.mapper import FieldMapper
from core.marketplace import MarketplaceGenerator, ProgressCallback, register
from core.models import (
    FieldMappingConfig,
    GenerationRequest,
    GenerationResult,
    Severity,
    SummaryStats,
)
from core.seo_generator import SEOGenerator
from core.shopify_reader import ShopifyReader
from core.utils import get_logger, load_json_config
from core.validator import ListingValidator
from core.variation_builder import VariationBuilder

logger = get_logger("pipeline")

_PURITY_RE = re.compile(r"\b(9|10|14|18|22|24)\s*K(?:T|ARAT)?\b", re.IGNORECASE)


@register
class AmazonIndiaPipeline(MarketplaceGenerator):
    """Converts a Shopify export into an Amazon India bulk listing file."""

    marketplace_id = "amazon_in"
    display_name = "Amazon India"

    def __init__(self, config_dir: Path | None = None) -> None:
        self._defaults: dict[str, Any] = load_json_config("defaults.json", config_dir)
        self._rules: dict[str, Any] = load_json_config("amazon_rules.json", config_dir)
        self._categories: dict[str, Any] = load_json_config(
            "categories.json", config_dir
        )
        self._mapping = FieldMappingConfig(
            **load_json_config("field_mapping.json", config_dir)
        )

    # ------------------------------------------------------------------ #

    def generate(
        self,
        request: GenerationRequest,
        progress: ProgressCallback | None = None,
    ) -> GenerationResult:
        """Run the full pipeline. Never raises for data problems - those
        land in the error report; only unrecoverable setup errors raise."""
        started = time.perf_counter()

        def report(fraction: float, message: str) -> None:
            logger.info("[%3d%%] %s", int(fraction * 100), message)
            if progress:
                progress(fraction, message)

        report(0.02, "Reading Shopify export...")
        shopify = ShopifyReader(request.shopify_csv).read()

        report(0.15, "Building parent/child variations...")
        listings = VariationBuilder(self._rules).build(shopify.variants)

        report(0.30, "Applying Lukson business rules...")
        listings = self._enrich(listings)

        report(0.40, "Mapping product images...")
        listings = ImageMapper(
            int(self._defaults.get("max_additional_images", 8))
        ).apply(listings, shopify.images)

        report(0.50, "Generating SEO titles, bullets and keywords...")
        listings = SEOGenerator(self._rules, self._defaults).generate(listings)

        report(0.62, "Mapping fields to the Amazon template...")
        mapper = FieldMapper(self._mapping, self._defaults)
        amazon = mapper.map(listings)
        amazon = self._blank_parent_fields(amazon, listings)

        report(0.72, "Validating listings...")
        issues = ListingValidator(self._rules).validate(amazon, listings)

        report(0.80, "Writing the Amazon upload file...")
        # Canonical field names double as header hints so the machine-name
        # header row always outscores the human display-name row above it.
        header_hints = self._mapping.header_row_hints + [
            rule.amazon_field for rule in mapper.rules
        ]
        template = AmazonTemplate(
            request.amazon_template,
            sheet_name=self._mapping.template_sheet,
            header_hints=header_hints,
            header_search_max_rows=self._mapping.header_search_max_rows,
        )
        writer = ExcelWriter()
        output_file, skipped_fields = writer.write_upload_file(
            template,
            amazon,
            mapper.rules,
            request.output_dir / str(self._defaults.get("output_file_name")),
        )

        report(0.92, "Writing reports...")
        stats = self._build_stats(
            shopify.total_rows, listings, amazon, issues, started
        )
        validation_report = writer.write_issue_report(
            issues,
            request.output_dir / str(self._defaults.get("validation_report_name")),
            "Validation",
        )
        error_report = writer.write_issue_report(
            issues,
            request.output_dir / str(self._defaults.get("error_report_name")),
            "Errors",
            errors_only=True,
        )
        summary_extra = {
            "Shopify File": str(request.shopify_csv),
            "Amazon Template": str(request.amazon_template),
            "Fields Not In Template": ", ".join(skipped_fields) or "None",
        }
        summary_report = writer.write_summary_report(
            stats,
            request.output_dir / str(self._defaults.get("summary_report_name")),
            summary_extra,
        )

        report(1.0, "Done")
        status = "success" if stats.errors == 0 else "completed_with_errors"
        message = (
            f"Generated {stats.total_listings} listings "
            f"({stats.parents} parents, {stats.children} children) "
            f"with {stats.errors} errors and {stats.warnings} warnings "
            f"in {stats.elapsed_seconds:.1f}s."
        )
        logger.info(message)
        return GenerationResult(
            status=status,
            output_file=output_file,
            validation_report=validation_report,
            error_report=error_report,
            summary_report=summary_report,
            stats=stats,
            message=message,
        )

    # ------------------------------------------------------------------ #

    def _enrich(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Add computed business columns (vectorized)."""
        frame = frame.copy()
        n = len(frame)

        def col(name: str) -> pd.Series:
            if name in frame.columns:
                return frame[name].astype(str).str.strip()
            return pd.Series([""] * n, index=frame.index)

        # --- category matching -------------------------------------------
        haystack = (
            col("Type") + " " + col("Title") + " " + col("Tags")
        ).str.lower()
        default_cat = self._categories.get("default", {})
        frame["_feed_product_type"] = default_cat.get("feed_product_type", "")
        frame["_product_type_label"] = default_cat.get("product_type_label", "")
        frame["_browse_node"] = default_cat.get("browse_node", "")
        unmatched = pd.Series(True, index=frame.index)
        for category in self._categories.get("categories", []):
            keywords = [re.escape(k.lower()) for k in category.get("match_keywords", [])]
            if not keywords:
                continue
            mask = unmatched & haystack.str.contains(
                "|".join(keywords), regex=True, na=False
            )
            frame.loc[mask, "_feed_product_type"] = category.get(
                "feed_product_type", ""
            )
            frame.loc[mask, "_product_type_label"] = category.get(
                "product_type_label", ""
            )
            frame.loc[mask, "_browse_node"] = category.get("browse_node", "")
            unmatched &= ~mask

        # --- price / stock / weight ---------------------------------------
        frame["_price"] = col("Variant Price")
        mrp = col("Variant Compare At Price")
        frame["_mrp"] = mrp.mask(mrp == "", frame["_price"])
        qty = pd.to_numeric(col("Variant Inventory Qty"), errors="coerce")
        frame["_quantity"] = (
            qty.fillna(float(self._defaults.get("default_quantity", 1)))
            .clip(lower=0)
            .astype(int)
            .astype(str)
        )
        grams = pd.to_numeric(col("Variant Grams"), errors="coerce")
        frame["_weight"] = np.where(grams.notna() & (grams > 0), grams, np.nan)
        frame["_weight"] = (
            pd.Series(frame["_weight"], index=frame.index)
            .map(lambda g: f"{g:g}" if pd.notna(g) else "")
        )

        # --- barcode type ---------------------------------------------------
        barcode = col("Variant Barcode").str.replace(r"\D", "", regex=True)
        frame["_external_id_type"] = np.select(
            [barcode.str.len() == 13, barcode.str.len() == 12, barcode.str.len() == 14],
            ["EAN", "UPC", "GTIN"],
            default="",
        )

        # --- purity fallback from title/tags --------------------------------
        if "_metal_stamp" not in frame.columns:
            frame["_metal_stamp"] = ""
        blank = frame["_metal_stamp"].astype(str).str.strip() == ""
        if blank.any():
            extracted = (
                (col("Title") + " " + col("Tags"))
                .str.extract(_PURITY_RE, expand=False)
                .fillna("")
            )
            frame.loc[blank, "_metal_stamp"] = np.where(
                extracted[blank] != "", extracted[blank] + "K", ""
            )
        frame["_metal_stamp"] = frame["_metal_stamp"].astype(str).str.upper().str.strip()

        # --- diamond / stone details from metafields -------------------------
        for target, candidates in self._rules.get("metafield_columns", {}).items():
            column = f"_{target}"
            frame[column] = ""
            for candidate in candidates:
                if candidate not in frame.columns:
                    continue
                values = frame[candidate].astype(str).str.strip()
                frame[column] = frame[column].mask(
                    (frame[column] == "") & (values != ""), values
                )
        if "_stone_type" in frame.columns:
            frame["_stone_type"] = frame["_stone_type"].mask(
                frame["_stone_type"] == "", str(self._defaults.get("stone_type", ""))
            )
        return frame

    def _blank_parent_fields(
        self, amazon: pd.DataFrame, listings: pd.DataFrame
    ) -> pd.DataFrame:
        """Parents must not carry sellable-only values (price, qty, ...)."""
        blanked = self._rules.get("variation", {}).get("parent_fields_blanked", [])
        is_parent = listings["_parentage"] == "parent"
        for field in blanked:
            if field in amazon.columns:
                amazon.loc[is_parent, field] = ""
        return amazon

    @staticmethod
    def _build_stats(
        total_rows: int,
        listings: pd.DataFrame,
        amazon: pd.DataFrame,
        issues: pd.DataFrame,
        started: float,
    ) -> SummaryStats:
        parentage = listings["_parentage"]
        errors = issues[issues["Severity"] == Severity.ERROR.value]
        rows_with_errors = errors["Row"].nunique()
        sellable = parentage != "parent"
        missing_images = int(
            (sellable & (amazon.get("main_image_url", "") == "")).sum()
        )
        return SummaryStats(
            total_shopify_rows=total_rows,
            total_products=int(listings["Handle"].nunique()),
            total_listings=len(listings),
            parents=int((parentage == "parent").sum()),
            children=int((parentage == "child").sum()),
            standalone=int((parentage == "").sum()),
            missing_images=missing_images,
            errors=len(errors),
            warnings=int((issues["Severity"] == Severity.WARNING.value).sum()),
            successful_listings=len(listings) - rows_with_errors,
            elapsed_seconds=time.perf_counter() - started,
        )
