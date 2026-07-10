"""Maps Shopify product images onto Amazon image slots.

Amazon accepts one main image plus up to eight additional image URLs.
Shopify stores images per product (ordered by ``Image Position``) and can
pin one image to a specific variant (``Variant Image``). Rules:

* Main image  = the variant's own image if set, else the product's first.
* Additional  = remaining product images, in Shopify's order, skipping the
  one already used as main.
"""

from __future__ import annotations

import pandas as pd

from core.utils import get_logger

logger = get_logger("image_mapper")


class ImageMapper:
    """Fills ``_main_image_url`` and ``_other_image_url_N`` columns."""

    def __init__(self, max_additional: int = 8) -> None:
        self._max_additional = max_additional

    def apply(self, listings: pd.DataFrame, images: pd.DataFrame) -> pd.DataFrame:
        """Attach image URL columns to every listing row.

        Args:
            listings: Parent/child listing table (has ``Handle``).
            images: Ordered image table from :class:`ShopifyReader`.

        Returns:
            The listing table with image columns populated in order.
        """
        listings = listings.copy()
        other_cols = [
            f"_other_image_url_{i}" for i in range(1, self._max_additional + 1)
        ]
        listings["_main_image_url"] = ""
        for col in other_cols:
            listings[col] = ""

        if images.empty:
            logger.warning("No images found in the Shopify export")
            return listings

        # Ordered list of image URLs per handle.
        urls_by_handle: dict[str, list[str]] = (
            images.groupby("Handle", sort=False)["Image Src"].agg(list).to_dict()
        )

        variant_image = (
            listings["Variant Image"].astype(str).str.strip()
            if "Variant Image" in listings.columns
            else pd.Series("", index=listings.index)
        )

        mains: list[str] = []
        others: list[list[str]] = []
        for handle, v_img in zip(listings["Handle"], variant_image):
            gallery = urls_by_handle.get(handle, [])
            main = v_img or (gallery[0] if gallery else "")
            rest = [u for u in gallery if u != main][: self._max_additional]
            mains.append(main)
            others.append(rest)

        listings["_main_image_url"] = mains
        for i, col in enumerate(other_cols):
            listings[col] = [row[i] if len(row) > i else "" for row in others]

        mapped = sum(1 for m in mains if m)
        logger.info("Mapped images: %d/%d rows have a main image", mapped, len(listings))
        return listings
