"""Validation engine for generated Amazon listing data.

All checks operate on vectorized boolean masks over the full listing
table, so validating 20k+ rows takes milliseconds. Every finding records
the 1-based data row number, the SKU, the offending field, a severity and
a human-readable message.
"""

from __future__ import annotations

import re
from typing import Any

import numpy as np
import pandas as pd

from core.models import Severity
from core.utils import get_logger

logger = get_logger("validator")

ISSUE_COLUMNS = ["Row", "SKU", "Field", "Severity", "Message"]

_PARENT = "parent"
_CHILD = "child"


class ListingValidator:
    """Runs the full validation suite over mapped listing data."""

    def __init__(self, rules: dict[str, Any]) -> None:
        validation = rules.get("validation", {})
        self._required_fields: list[str] = validation.get("required_fields", [])
        self._required_child_fields: list[str] = validation.get(
            "required_child_fields", []
        )
        self._image_url_re = re.compile(
            validation.get("image_url_pattern", r"^https?://\S+$"), re.IGNORECASE
        )
        self._sku_re = re.compile(validation.get("sku_pattern", r"^\S{1,40}$"))
        self._price_min = float(validation.get("price_min", 0))
        self._price_max = float(validation.get("price_max", 1e9))

    # ------------------------------------------------------------------ #

    def validate(self, amazon: pd.DataFrame, working: pd.DataFrame) -> pd.DataFrame:
        """Validate the mapped output against Amazon and Lukson rules.

        Args:
            amazon: Mapped table (columns are canonical Amazon field names).
            working: Enriched working table aligned row-for-row with
                ``amazon`` (provides ``_parentage`` etc.).

        Returns:
            DataFrame of issues with columns Row/SKU/Field/Severity/Message.
        """
        issues: list[pd.DataFrame] = []
        rows = pd.Series(np.arange(1, len(amazon) + 1), index=amazon.index)
        sku = self._col(amazon, "item_sku")
        parentage = self._col(working, "_parentage")
        is_child = parentage == _CHILD
        is_parent = parentage == _PARENT
        is_sellable = ~is_parent  # children + standalone products

        def add(mask: pd.Series, field: str, severity: Severity, message: str) -> None:
            if not mask.any():
                return
            issues.append(
                pd.DataFrame(
                    {
                        "Row": rows[mask].to_numpy(),
                        "SKU": sku[mask].to_numpy(),
                        "Field": field,
                        "Severity": severity.value,
                        "Message": message,
                    }
                )
            )

        # --- SKU checks -------------------------------------------------
        add(sku == "", "item_sku", Severity.ERROR, "Missing SKU")
        bad_sku = (sku != "") & ~sku.str.match(self._sku_re)
        add(bad_sku, "item_sku", Severity.ERROR, "SKU contains invalid characters")
        dup = (sku != "") & sku.duplicated(keep=False)
        add(dup, "item_sku", Severity.ERROR, "Duplicate SKU")

        # --- Mandatory Amazon fields ------------------------------------
        for field in self._required_fields:
            if field == "item_sku":
                continue  # already covered
            values = self._col(amazon, field)
            add(
                values == "",
                field,
                Severity.ERROR,
                f"Missing mandatory Amazon field '{field}'",
            )
        for field in self._required_child_fields:
            values = self._col(amazon, field)
            add(
                is_sellable & (values == ""),
                field,
                Severity.ERROR,
                f"Missing '{field}' (required for sellable listings)",
            )

        # --- Price -------------------------------------------------------
        price_text = self._col(amazon, "standard_price")
        price = pd.to_numeric(price_text, errors="coerce")
        add(
            is_sellable & (price_text != "") & price.isna(),
            "standard_price",
            Severity.ERROR,
            "Price is not a number",
        )
        out_of_range = is_sellable & price.notna() & (
            (price < self._price_min) | (price > self._price_max)
        )
        add(
            out_of_range,
            "standard_price",
            Severity.ERROR,
            f"Price outside allowed range {self._price_min:g}-{self._price_max:g}",
        )

        # --- Weight / purity / diamond details (Lukson rules) ------------
        add(
            is_sellable & (self._col(amazon, "item_weight") == ""),
            "item_weight",
            Severity.WARNING,
            "Missing product weight",
        )
        add(
            is_sellable & (self._col(amazon, "metal_stamp") == ""),
            "metal_stamp",
            Severity.ERROR,
            "Missing metal purity (metal stamp)",
        )
        stone = self._col(amazon, "stone_type")
        stone_weight = self._col(amazon, "stone_weight")
        add(
            is_sellable & (stone != "") & (stone_weight == ""),
            "stone_weight",
            Severity.WARNING,
            "Missing diamond details (stone weight)",
        )

        # --- Images -------------------------------------------------------
        main_image = self._col(amazon, "main_image_url")
        add(
            is_sellable & (main_image == ""),
            "main_image_url",
            Severity.ERROR,
            "Missing main image",
        )
        image_cols = ["main_image_url"] + [f"other_image_url{i}" for i in range(1, 9)]
        for field in image_cols:
            urls = self._col(amazon, field)
            invalid = (urls != "") & ~urls.str.match(self._image_url_re)
            add(invalid, field, Severity.ERROR, "Invalid image URL")

        # --- Description ---------------------------------------------------
        add(
            self._col(amazon, "product_description") == "",
            "product_description",
            Severity.WARNING,
            "Missing product description",
        )

        # --- Variation integrity -------------------------------------------
        issues.extend(
            self._variation_checks(amazon, working, rows, sku, is_child, is_parent)
        )

        report = (
            pd.concat(issues, ignore_index=True)
            if issues
            else pd.DataFrame(columns=ISSUE_COLUMNS)
        )
        report = report[ISSUE_COLUMNS].sort_values(
            ["Severity", "Row"], kind="stable", ignore_index=True
        )
        logger.info(
            "Validation finished: %d errors, %d warnings",
            int((report["Severity"] == Severity.ERROR.value).sum()),
            int((report["Severity"] == Severity.WARNING.value).sum()),
        )
        return report

    # ------------------------------------------------------------------ #

    def _variation_checks(
        self,
        amazon: pd.DataFrame,
        working: pd.DataFrame,
        rows: pd.Series,
        sku: pd.Series,
        is_child: pd.Series,
        is_parent: pd.Series,
    ) -> list[pd.DataFrame]:
        """Parent/child relationship consistency checks."""
        results: list[pd.DataFrame] = []
        parent_sku = self._col(amazon, "parent_sku")
        theme = self._col(amazon, "variation_theme")
        relationship = self._col(amazon, "relationship_type")

        def add(mask: pd.Series, field: str, severity: Severity, message: str) -> None:
            if not mask.any():
                return
            results.append(
                pd.DataFrame(
                    {
                        "Row": rows[mask].to_numpy(),
                        "SKU": sku[mask].to_numpy(),
                        "Field": field,
                        "Severity": severity.value,
                        "Message": message,
                    }
                )
            )

        known_parents = set(sku[is_parent])
        add(
            is_child & (parent_sku == ""),
            "parent_sku",
            Severity.ERROR,
            "Child listing has no parent SKU",
        )
        add(
            is_child & (parent_sku != "") & ~parent_sku.isin(known_parents),
            "parent_sku",
            Severity.ERROR,
            "Invalid parent relationship: parent SKU does not exist",
        )
        add(
            is_child & (theme == ""),
            "variation_theme",
            Severity.ERROR,
            "Invalid variation: child has no variation theme",
        )
        add(
            is_child & (relationship == ""),
            "relationship_type",
            Severity.ERROR,
            "Invalid variation: child has no relationship type",
        )
        add(
            is_parent & (theme == ""),
            "variation_theme",
            Severity.ERROR,
            "Invalid variation: parent has no variation theme",
        )
        # A parent whose children all disappeared is invalid.
        child_parent_counts = parent_sku[is_child].value_counts()
        orphan_parent = is_parent & ~sku.isin(child_parent_counts.index)
        add(
            orphan_parent,
            "item_sku",
            Severity.ERROR,
            "Invalid parent relationship: parent has no child listings",
        )
        return results

    @staticmethod
    def _col(frame: pd.DataFrame, name: str) -> pd.Series:
        """Return a stripped string column, or an empty one if absent."""
        if name in frame.columns:
            return frame[name].astype(str).str.strip()
        return pd.Series([""] * len(frame), index=frame.index, dtype=str)
