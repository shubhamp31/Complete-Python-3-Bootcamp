"""Reader for Shopify product export CSV files.

A Shopify export contains one row per variant, plus extra rows that only
carry additional images. Product-level columns (Title, Body, Vendor, ...)
are populated only on the first row of each product. This module turns
that layout into two clean DataFrames:

* ``variants`` - one row per sellable variant, with all product-level
  fields forward-filled and options pivoted into named columns.
* ``images``   - one row per (handle, image) pair, ordered by position.

Everything is done with vectorized pandas operations so 20k+ row exports
are processed in seconds.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from core.utils import InputFileError, get_logger

logger = get_logger("shopify_reader")

#: Product-level columns that Shopify only writes on the first row of a
#: product and that must be forward-filled across its variant rows.
PRODUCT_LEVEL_COLUMNS: tuple[str, ...] = (
    "Title",
    "Body (HTML)",
    "Vendor",
    "Product Category",
    "Type",
    "Tags",
    "Published",
    "Option1 Name",
    "Option2 Name",
    "Option3 Name",
    "SEO Title",
    "SEO Description",
    "Status",
)

REQUIRED_COLUMNS: tuple[str, ...] = ("Handle",)

#: Columns that indicate a row describes a sellable variant (as opposed to
#: an image-only continuation row).
_VARIANT_SIGNAL_COLUMNS: tuple[str, ...] = (
    "Variant SKU",
    "Variant Price",
    "Option1 Value",
)


@dataclass(frozen=True)
class ShopifyData:
    """Parsed Shopify export."""

    variants: pd.DataFrame
    images: pd.DataFrame
    total_rows: int

    @property
    def product_count(self) -> int:
        """Number of distinct products (handles) in the export."""
        return int(self.variants["Handle"].nunique())


class ShopifyReader:
    """Parses Shopify product export CSVs of arbitrary size."""

    def __init__(self, csv_path: Path | str) -> None:
        self._path = Path(csv_path)

    def read(self) -> ShopifyData:
        """Read and normalise the export.

        Returns:
            ShopifyData with variant and image tables.

        Raises:
            InputFileError: If the file is unreadable or not a Shopify export.
        """
        raw = self._read_csv()
        total_rows = len(raw)
        logger.info("Read %d rows from %s", total_rows, self._path.name)

        self._require_columns(raw)
        raw = self._forward_fill_product_fields(raw)

        images = self._extract_images(raw)
        variants = self._extract_variants(raw)

        logger.info(
            "Parsed %d variants across %d products, %d images",
            len(variants),
            variants["Handle"].nunique(),
            len(images),
        )
        return ShopifyData(variants=variants, images=images, total_rows=total_rows)

    # ------------------------------------------------------------------ #

    def _read_csv(self) -> pd.DataFrame:
        """Load the CSV with all values as strings (no dtype surprises)."""
        if not self._path.is_file():
            raise InputFileError(f"Shopify CSV not found: {self._path}")
        try:
            frame = pd.read_csv(
                self._path,
                dtype=str,
                keep_default_na=False,
                na_filter=False,
                encoding="utf-8-sig",
                on_bad_lines="warn",
                engine="c",
                low_memory=False,
            )
        except UnicodeDecodeError:
            frame = pd.read_csv(
                self._path,
                dtype=str,
                keep_default_na=False,
                na_filter=False,
                encoding="latin-1",
                on_bad_lines="warn",
                low_memory=False,
            )
        except Exception as exc:  # pragma: no cover - pandas-specific failures
            raise InputFileError(f"Could not read Shopify CSV: {exc}") from exc

        frame.columns = [str(c).strip() for c in frame.columns]
        return frame

    def _require_columns(self, frame: pd.DataFrame) -> None:
        missing = [c for c in REQUIRED_COLUMNS if c not in frame.columns]
        if missing:
            raise InputFileError(
                "This does not look like a Shopify product export. "
                f"Missing required column(s): {', '.join(missing)}"
            )

    def _forward_fill_product_fields(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Forward-fill product-level columns within each product (handle).

        Shopify writes these values only on a product's first row; every
        subsequent variant/image row leaves them blank.
        """
        frame = frame.copy()
        fill_cols = [c for c in PRODUCT_LEVEL_COLUMNS if c in frame.columns]
        # Product metafields (e.g. diamond details) are also written only on
        # the first row of each product.
        fill_cols += [
            c
            for c in frame.columns
            if "metafields" in c.lower() and c not in fill_cols
        ]
        if not fill_cols:
            return frame
        block = frame[fill_cols].replace("", np.nan)
        frame[fill_cols] = (
            block.groupby(frame["Handle"], sort=False).ffill().fillna("")
        )
        return frame

    def _extract_images(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Build the ordered (Handle, Image Src, Image Position) table."""
        cols = ["Handle"]
        for col in ("Image Src", "Image Position", "Variant Image"):
            if col in frame.columns:
                cols.append(col)

        if "Image Src" not in cols:
            return pd.DataFrame(columns=["Handle", "Image Src", "Image Position"])

        images = frame[cols].copy()
        images = images[images["Image Src"].astype(str).str.strip() != ""]
        if "Image Position" in images.columns:
            images["Image Position"] = pd.to_numeric(
                images["Image Position"], errors="coerce"
            ).fillna(9999)
        else:
            images["Image Position"] = np.arange(len(images))
        images = images.drop_duplicates(subset=["Handle", "Image Src"])
        images = images.sort_values(["Handle", "Image Position"], kind="stable")
        return images.reset_index(drop=True)

    def _extract_variants(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Keep only sellable variant rows and pivot options into columns."""
        signals = [c for c in _VARIANT_SIGNAL_COLUMNS if c in frame.columns]
        if signals:
            mask = np.zeros(len(frame), dtype=bool)
            for col in signals:
                mask |= frame[col].astype(str).str.strip() != ""
            # Always keep the first row of every product even if sparse,
            # so single-variant products without SKUs still surface.
            first_of_handle = ~frame["Handle"].duplicated()
            variants = frame[mask | first_of_handle].copy()
            # A first-of-handle row that is image-only (no variant signals
            # at all) is dropped again if the product has real variant rows.
            has_signal = mask[variants.index.to_numpy()] if len(variants) else mask
            handle_has_signal = (
                pd.Series(has_signal, index=variants.index)
                .groupby(variants["Handle"], sort=False)
                .transform("any")
            )
            keep = pd.Series(has_signal, index=variants.index) | ~handle_has_signal
            variants = variants[keep]
        else:
            variants = frame.copy()

        variants = variants.reset_index(drop=True)
        self._pivot_options(variants)
        return variants

    @staticmethod
    def _pivot_options(variants: pd.DataFrame) -> None:
        """Add ``_opt_<option name>`` columns from Option1..3 Name/Value pairs.

        For example a product with ``Option1 Name = "Metal Purity"`` gets a
        ``_opt_metal purity`` column holding each variant's purity value.
        Mutates ``variants`` in place (vectorized per option slot).
        """
        for slot in (1, 2, 3):
            name_col, value_col = f"Option{slot} Name", f"Option{slot} Value"
            if name_col not in variants.columns or value_col not in variants.columns:
                continue
            names = variants[name_col].astype(str).str.strip().str.lower()
            values = variants[value_col].astype(str).str.strip()
            valid = (names != "") & (~names.isin(("title", "default title")))
            for option_name in names[valid].unique():
                target = f"_opt_{option_name}"
                selection = valid & (names == option_name)
                if target not in variants.columns:
                    variants[target] = ""
                variants.loc[selection, target] = values[selection]
