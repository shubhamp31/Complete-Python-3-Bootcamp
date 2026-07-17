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

#: Diamond carat mentions in free text ("1.162 ct", "0.5 carat"), excluding
#: gold purity phrases such as "18 ct gold".
_CARAT_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:ct|cts|carats?)\b(?!\s*(?:gold|yellow|white|rose))",
    re.IGNORECASE,
)


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

        # Products explicitly excluded via configuration (e.g. flagged by an
        # Amazon processing report and awaiting a fix in Shopify).
        excluded_handles = {
            str(h).strip().lower()
            for h in self._defaults.get("excluded_handles", [])
            if str(h).strip()
        }
        variants = shopify.variants
        if excluded_handles:
            drop = variants["Handle"].astype(str).str.lower().isin(excluded_handles)
            if drop.any():
                logger.warning(
                    "Excluding %d configured product(s) (%d variants): %s",
                    variants.loc[drop, "Handle"].nunique(),
                    int(drop.sum()),
                    ", ".join(sorted(variants.loc[drop, "Handle"].unique())),
                )
                variants = variants[~drop].reset_index(drop=True)

        report(0.12, "Classifying products...")
        variants = self._categorise(variants)

        report(0.15, "Building parent/child variations...")
        listings = VariationBuilder(self._rules).build(variants)

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

        # The template declares which product types it supports; products
        # outside that list would be rejected by Amazon, so they are set
        # aside and reported instead of being written with a wrong type.
        supported = AmazonTemplate(
            request.amazon_template,
            sheet_name=self._mapping.template_sheet,
            header_hints=self._mapping.header_row_hints,
            header_search_max_rows=self._mapping.header_search_max_rows,
        ).supported_product_types()
        excluded_issues = pd.DataFrame(columns=["Row", "SKU", "Field", "Severity", "Message"])
        excluded_count = 0
        if supported:
            unsupported = ~listings["_feed_product_type"].astype(str).str.upper().isin(
                supported
            )
            excluded_count = int(unsupported.sum())
            if excluded_count:
                excluded = listings[unsupported]
                logger.warning(
                    "Excluding %d listings whose product type is not in the "
                    "template (%s); affected types: %s",
                    excluded_count,
                    ", ".join(supported),
                    ", ".join(sorted(excluded["_feed_product_type"].unique())),
                )
                excluded_issues = pd.DataFrame(
                    {
                        "Row": 0,
                        "SKU": excluded["_sku"].to_numpy(),
                        "Field": "feed_product_type",
                        "Severity": "Warning",
                        "Message": (
                            "Product type '"
                            + excluded["_feed_product_type"].astype(str)
                            + "' is not included in this Amazon template "
                            f"(supports: {', '.join(supported)}) - download a "
                            "template that covers it and regenerate"
                        ),
                    }
                )
                listings = listings[~unsupported].reset_index(drop=True)

        amazon = mapper.map(listings)
        amazon = self._blank_parent_fields(amazon, listings)

        report(0.72, "Validating listings...")
        issues = ListingValidator(self._rules).validate(amazon, listings)
        issues = pd.concat([issues, excluded_issues], ignore_index=True)

        report(0.80, "Writing the Amazon upload file...")
        # Canonical field names double as header hints so the machine-name
        # header row always outscores the human display-name row above it.
        header_hints = self._mapping.header_row_hints + [
            rule.amazon_field for rule in mapper.rules
        ]
        writer = ExcelWriter()
        max_rows = int(self._defaults.get("max_rows_per_upload", 9500))
        chunks = self._chunk_bounds(listings["Handle"], max_rows)
        base_name = Path(str(self._defaults.get("output_file_name")))
        output_files: list[Path] = []
        skipped_fields: list[str] = []
        for part, (start, end) in enumerate(chunks, start=1):
            template = AmazonTemplate(
                request.amazon_template,
                sheet_name=self._mapping.template_sheet,
                header_hints=header_hints,
                header_search_max_rows=self._mapping.header_search_max_rows,
            )
            name = (
                base_name.name
                if len(chunks) == 1
                else f"{base_name.stem} - Part {part}{base_name.suffix}"
            )
            path, skipped = writer.write_upload_file(
                template,
                amazon.iloc[start:end],
                mapper.rules,
                request.output_dir / name,
            )
            output_files.append(path)
            skipped_fields = skipped
        output_file = output_files[0]

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
            "Excluded (product type not in template)": excluded_count,
        }
        summary_report = writer.write_summary_report(
            stats,
            request.output_dir / str(self._defaults.get("summary_report_name")),
            summary_extra,
        )

        report(1.0, "Done")
        status = "success" if stats.errors == 0 else "completed_with_errors"
        parts_note = (
            f" across {len(output_files)} upload files" if len(output_files) > 1 else ""
        )
        message = (
            f"Generated {stats.total_listings} listings "
            f"({stats.parents} parents, {stats.children} children){parts_note} "
            f"with {stats.errors} errors and {stats.warnings} warnings "
            f"in {stats.elapsed_seconds:.1f}s."
        )
        logger.info(message)
        return GenerationResult(
            status=status,
            output_file=output_file,
            output_files=output_files,
            validation_report=validation_report,
            error_report=error_report,
            summary_report=summary_report,
            stats=stats,
            message=message,
        )

    # ------------------------------------------------------------------ #

    def _categorise(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Match products to categories: feed product type, browse node and
        per-category Amazon field values. Runs before variation building so
        the builder can apply per-product-type theme rules."""
        frame = frame.copy()
        n = len(frame)

        def col(name: str) -> pd.Series:
            if name in frame.columns:
                return frame[name].astype(str).str.strip()
            return pd.Series([""] * n, index=frame.index)

        haystack = (
            col("Type") + " " + col("Title") + " " + col("Tags")
        ).str.lower()
        default_cat = self._categories.get("default", {})
        category_fields = sorted(
            {
                key
                for cat in [default_cat, *self._categories.get("categories", [])]
                for key in cat.get("amazon_fields", {})
            }
        )
        frame["_feed_product_type"] = default_cat.get("feed_product_type", "")
        frame["_product_type_label"] = default_cat.get("product_type_label", "")
        frame["_browse_node"] = default_cat.get("browse_node", "")
        for field in category_fields:
            frame[f"_cat_{field}"] = str(
                default_cat.get("amazon_fields", {}).get(field, "")
            )
        unmatched = pd.Series(True, index=frame.index)
        for category in self._categories.get("categories", []):
            keywords = [re.escape(k.lower()) for k in category.get("match_keywords", [])]
            if not keywords:
                continue
            # Word boundaries with optional plural so "ring" matches
            # "Rings" but never the inside of "Earrings".
            pattern = rf"\b(?:{'|'.join(keywords)})s?\b"
            mask = unmatched & haystack.str.contains(pattern, regex=True, na=False)
            frame.loc[mask, "_feed_product_type"] = category.get(
                "feed_product_type", ""
            )
            frame.loc[mask, "_product_type_label"] = category.get(
                "product_type_label", ""
            )
            frame.loc[mask, "_browse_node"] = category.get("browse_node", "")
            for field, value in category.get("amazon_fields", {}).items():
                frame.loc[mask, f"_cat_{field}"] = str(value)
            unmatched &= ~mask
        return frame

    def _enrich(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Add computed business columns (vectorized)."""
        frame = frame.copy()
        n = len(frame)

        def col(name: str) -> pd.Series:
            if name in frame.columns:
                return frame[name].astype(str).str.strip()
            return pd.Series([""] * n, index=frame.index)

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
        # Fallback: weight metafields (numeric, or configured range labels
        # such as "2-5-g" mapped to representative values).
        weight_cfg = self._rules.get("weight", {})
        weight_map = {
            str(k).lower(): str(v) for k, v in weight_cfg.get("value_map", {}).items()
        }
        for candidate in weight_cfg.get("metafield_columns", []):
            if candidate not in frame.columns:
                continue
            raw = frame[candidate].astype(str).str.strip()
            numeric = pd.to_numeric(raw, errors="coerce").map(
                lambda g: f"{g:g}" if pd.notna(g) and g > 0 else ""
            )
            mapped = raw.str.lower().map(weight_map).fillna("")
            fallback = numeric.mask(numeric == "", mapped)
            frame["_weight"] = frame["_weight"].mask(
                (frame["_weight"] == "") & (fallback != ""), fallback
            )
        default_weight = str(weight_cfg.get("default_value", ""))
        if default_weight:
            frame["_weight"] = frame["_weight"].mask(
                frame["_weight"] == "", default_weight
            )

        # --- barcode type ---------------------------------------------------
        barcode = col("Variant Barcode").str.replace(r"\D", "", regex=True)
        frame["_external_id_type"] = np.select(
            [barcode.str.len() == 13, barcode.str.len() == 12, barcode.str.len() == 14],
            ["EAN", "UPC", "GTIN"],
            default="",
        )

        # --- gold colour option: split "14K Yellow Gold" -------------------
        # Shopify stores purity+colour together (e.g. "9K Rose Gold"); Amazon
        # wants metal_stamp ("9K"), metal_type ("Rose Gold") and a colour that
        # stays unique per variant (the full string).
        if "_color" not in frame.columns:
            frame["_color"] = ""
        color = (
            frame["_color"].astype(str).str.strip()
            # Fix stray trailing digits from typos, e.g. "18K Yellow Gold1".
            .str.replace(r"([A-Za-z])\d+$", r"\1", regex=True)
            .str.strip()
        )
        karat = color.str.extract(r"^\s*(\d{1,2})\s*[Kk]?\b", expand=False).fillna("")
        option_purity = np.where(karat != "", karat + "K", "")
        metal_part = (
            color.str.replace(r"^\s*\d{1,2}\s*[Kk]?(?:t|T)?\b", "", regex=True)
            .str.strip()
            .str.title()
        )
        frame["_metal_type"] = np.where(
            metal_part != "", metal_part, str(self._defaults.get("metal_type", "Gold"))
        )
        frame["_color"] = np.where(
            (option_purity != "") & (metal_part != ""),
            pd.Series(option_purity, index=frame.index) + " " + metal_part,
            color,
        )

        # --- purity: option value, then title/tags fallback -----------------
        if "_metal_stamp" not in frame.columns:
            frame["_metal_stamp"] = ""
        stamp = frame["_metal_stamp"].astype(str).str.strip()
        # A raw "gold colour" value may have landed in metal stamp via the
        # Option1 fallback; extract just the karat portion from it.
        stamp_karat = stamp.str.extract(
            r"^\s*(\d{1,2})\s*[Kk]?\b", expand=False
        ).fillna("")
        stamp = np.where(stamp_karat != "", stamp_karat + "K", stamp)
        stamp = np.where(stamp == "", option_purity, stamp)
        frame["_metal_stamp"] = pd.Series(stamp, index=frame.index)
        is_parent = (
            frame["_parentage"] == "parent"
            if "_parentage" in frame.columns
            else pd.Series(False, index=frame.index)
        )
        blank = (frame["_metal_stamp"] == "") & ~is_parent
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
        # Parents must not carry a specific purity - it varies per child.
        # (Colour-split family parents keep theirs: one colour per parent.)
        split = (
            frame["_family_split"]
            if "_family_split" in frame.columns
            else pd.Series(False, index=frame.index)
        )
        frame.loc[
            is_parent & (frame["_variation_theme"] != "") & ~split, "_metal_stamp"
        ] = ""

        # --- ring size: "05 IND" -> "5" -------------------------------------
        if "_ring_size" in frame.columns:
            frame["_ring_size"] = (
                frame["_ring_size"].astype(str)
                .str.replace(r"(?i)\s*IND\.?\s*$", "", regex=True)
                .str.strip()
                .str.replace(r"^0+(\d)", r"\1", regex=True)
            )

        # --- parentage label for the template ("Parent"/"Child") ------------
        frame["_parentage_label"] = (
            frame["_parentage"].map({"parent": "Parent", "child": "Child"}).fillna("")
            if "_parentage" in frame.columns
            else ""
        )

        # --- occasion: metafield -> Amazon valid values ----------------------
        occasion_cfg = self._rules.get("occasion", {})
        frame["_occasion"] = ""
        value_map = {
            str(k).lower(): v for k, v in occasion_cfg.get("value_map", {}).items()
        }
        for candidate in occasion_cfg.get("columns", []):
            if candidate not in frame.columns:
                continue
            mapped = (
                frame[candidate].astype(str).str.strip().str.lower().map(value_map)
            ).fillna("")
            frame["_occasion"] = frame["_occasion"].mask(
                (frame["_occasion"] == "") & (mapped != ""), mapped
            )

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
        if "_stone_shape" in frame.columns:
            frame["_stone_shape"] = frame["_stone_shape"].astype(str).str.title()
        default_shape = str(self._rules.get("stone", {}).get("default_shape", ""))
        if default_shape and "_stone_shape" in frame.columns:
            frame["_stone_shape"] = frame["_stone_shape"].mask(
                frame["_stone_shape"] == "", default_shape
            )

        # --- stone carats / colour (required by the jewellery template) -----
        stone_cfg = self._rules.get("stone", {})
        carat_map = {
            str(k).lower(): str(v)
            for k, v in stone_cfg.get("carat_value_map", {}).items()
        }
        if "_stone_weight" not in frame.columns:
            frame["_stone_weight"] = ""
        frame["_total_diamond_weight"] = ""
        # Exact carats from product copy (SEO description, body HTML, ...):
        # the first mention is the centre stone, the largest is the total.
        text_cols = [
            c for c in stone_cfg.get("carat_text_columns", []) if c in frame.columns
        ]
        if text_cols:
            stone_by_handle: dict[str, str] = {}
            total_by_handle: dict[str, str] = {}
            products = frame.drop_duplicates("Handle")
            for row in products[["Handle", *text_cols]].itertuples(index=False):
                handle = row[0]
                for text in row[1:]:
                    values = [
                        float(v)
                        for v in _CARAT_RE.findall(str(text))
                        if 0.05 <= float(v) <= 6
                    ]
                    if values:
                        stone_by_handle[handle] = f"{values[0]:g}"
                        total_by_handle[handle] = f"{max(values):g}"
                        break
            extracted = frame["Handle"].map(stone_by_handle).fillna("")
            frame["_stone_weight"] = frame["_stone_weight"].mask(
                (frame["_stone_weight"] == "") & (extracted != ""), extracted
            )
            frame["_total_diamond_weight"] = (
                frame["Handle"].map(total_by_handle).fillna("")
            )
            logger.info(
                "Extracted exact carats from product text for %d products",
                len(stone_by_handle),
            )
        for candidate in stone_cfg.get("carat_range_columns", []):
            if candidate not in frame.columns:
                continue
            raw = frame[candidate].astype(str).str.strip()
            numeric = pd.to_numeric(raw, errors="coerce").map(
                lambda c: f"{c:g}" if pd.notna(c) and c > 0 else ""
            )
            mapped = raw.str.lower().map(carat_map).fillna("")
            fallback = numeric.mask(numeric == "", mapped)
            frame["_stone_weight"] = frame["_stone_weight"].mask(
                (frame["_stone_weight"] == "") & (fallback != ""), fallback
            )
        default_carat = str(stone_cfg.get("default_carat", ""))
        if default_carat:
            frame["_stone_weight"] = frame["_stone_weight"].mask(
                frame["_stone_weight"] == "", default_carat
            )
        frame["_total_diamond_weight"] = frame["_total_diamond_weight"].mask(
            frame["_total_diamond_weight"] == "", frame["_stone_weight"]
        )
        frame["_stone_weight_unit"] = np.where(
            frame["_stone_weight"] != "", str(stone_cfg.get("weight_unit", "Carats")), ""
        )
        frame["_total_diamond_weight_unit"] = np.where(
            frame["_total_diamond_weight"] != "",
            str(stone_cfg.get("total_weight_unit", "carats")),
            "",
        )
        if "_stone_color" not in frame.columns:
            frame["_stone_color"] = ""
        default_color = str(stone_cfg.get("default_color", ""))
        if default_color:
            frame["_stone_color"] = frame["_stone_color"].mask(
                frame["_stone_color"] == "", default_color
            )

        # --- units only where a value exists ---------------------------------
        frame["_weight_unit"] = np.where(
            frame["_weight"] != "",
            str(self._defaults.get("item_weight_unit_of_measure", "Grams")),
            "",
        )

        # --- ring size fallback for rings sold without a size option ---------
        ring_fallback = str(self._rules.get("ring_size_fallback", ""))
        if ring_fallback and "_ring_size" in frame.columns:
            needs_size = (
                frame["_feed_product_type"].astype(str).str.upper().isin(
                    ("RING", "FINERING")
                )
                & (frame["_parentage"] != "parent")
                & (frame["_ring_size"] == "")
            )
            frame.loc[needs_size, "_ring_size"] = ring_fallback
        return frame

    def _blank_parent_fields(
        self, amazon: pd.DataFrame, listings: pd.DataFrame
    ) -> pd.DataFrame:
        """Parents must not carry sellable-only values (price, qty, ...).

        Colour/purity fields stay on parents of colour-split families -
        each of those parents represents exactly one colour.
        """
        blanked = self._rules.get("variation", {}).get("parent_fields_blanked", [])
        is_parent = listings["_parentage"] == "parent"
        split = (
            listings["_family_split"]
            if "_family_split" in listings.columns
            else pd.Series(False, index=listings.index)
        )
        colour_fields = {
            "color_name", "metal_stamp", "metal_type",
            "metals_id", "metals_metal_type", "metals_metal_stamp",
        }
        for field in blanked:
            if field in amazon.columns:
                mask = is_parent & ~split if field in colour_fields else is_parent
                amazon.loc[mask, field] = ""
        return amazon

    @staticmethod
    def _chunk_bounds(handles: pd.Series, max_rows: int) -> list[tuple[int, int]]:
        """Split row positions into chunks of at most ``max_rows``.

        Chunks only break between products (handles), so a parent and its
        children always land in the same upload file. A single family
        larger than ``max_rows`` still gets its own (oversized) chunk.
        """
        runs = handles.groupby((handles != handles.shift()).cumsum(), sort=False).size()
        bounds: list[tuple[int, int]] = []
        start = pos = 0
        current = 0
        for size in runs:
            if current and current + size > max_rows:
                bounds.append((start, pos))
                start, current = pos, 0
            current += int(size)
            pos += int(size)
        if current:
            bounds.append((start, pos))
        return bounds or [(0, 0)]

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
