"""Builds Amazon parent/child variation structures from Shopify variants.

Shopify expresses variations as option columns (Ring Size, Metal Purity,
Color, ...). Amazon expects an explicit parent row plus child rows linked
by ``parent_sku`` / ``relationship_type`` / ``variation_theme``. This module
performs that transformation with vectorized pandas operations.
"""

from __future__ import annotations

import zlib
from typing import Any

import numpy as np
import pandas as pd

from core.utils import get_logger, slugify

logger = get_logger("variation_builder")

#: Shopify option name (lowercased) -> internal attribute column.
DEFAULT_OPTION_ATTRIBUTES: dict[str, str] = {
    "ring size": "_ring_size",
    "size": "_ring_size",
    "metal purity": "_metal_stamp",
    "purity": "_metal_stamp",
    "metal": "_metal_stamp",
    "karat": "_metal_stamp",
    "carat": "_metal_stamp",
    "color": "_color",
    "colour": "_color",
    "finish": "_color",
    "weight": "_weight_option",
}

_PARENT = "parent"
_CHILD = "child"


class VariationBuilder:
    """Creates parent/child rows and variation metadata."""

    def __init__(self, rules: dict[str, Any]) -> None:
        variation = rules.get("variation", {})
        #: What to do when the Shopify export assigns one SKU to multiple
        #: variants: "suffix_others" (first variant keeps the original SKU,
        #: the rest get option-derived suffixes), "suffix" (every variant in
        #: a duplicate group gets a suffix), "keep" (leave untouched;
        #: validation flags them) or "drop" (keep the first variant per SKU
        #: and discard the rest).
        self._sku_duplicate_strategy: str = str(
            variation.get("sku_duplicate_strategy", "suffix_others")
        ).lower()
        self._themes: dict[str, str] = {
            k.lower(): v for k, v in variation.get("themes", {}).items()
        }
        # Re-key combined themes on sorted parts so lookup order never matters.
        self._combined: dict[str, str] = {
            "|".join(sorted(key.split("|"))): value
            for key, value in variation.get("combined_themes", {}).items()
        }
        self._relationship_type: str = variation.get("relationship_type", "Variation")
        self._parent_prefix: str = variation.get("parent_sku_prefix", "P-")
        self._option_attributes: dict[str, str] = {
            k.lower(): v
            for k, v in variation.get(
                "option_attributes", DEFAULT_OPTION_ATTRIBUTES
            ).items()
        }
        #: Per product type: the variation themes Amazon accepts. When a
        #: product's combined theme (e.g. COLOR/RING_SIZE) is not allowed,
        #: the family is split by one option so the remaining theme is.
        self._allowed_themes: dict[str, set[str]] = {
            pt.upper(): {t.upper() for t in themes}
            for pt, themes in variation.get("allowed_themes", {}).items()
        }

    # ------------------------------------------------------------------ #

    def build(self, variants: pd.DataFrame) -> pd.DataFrame:
        """Return a listing table of parents + children (+ standalones).

        Args:
            variants: One row per Shopify variant, including ``_opt_*``
                option columns produced by :class:`ShopifyReader`.

        Returns:
            DataFrame with the original variant columns plus ``_sku``,
            ``_parent_sku``, ``_parentage``, ``_relationship_type``,
            ``_variation_theme`` and mapped option attribute columns.
            Parent rows precede their children.
        """
        if variants.empty:
            return variants.assign(
                _sku="", _parent_sku="", _parentage="",
                _relationship_type="", _variation_theme="",
            )

        frame = variants.copy()
        self._assign_skus(frame)
        if self._sku_duplicate_strategy == "drop":
            duplicated = (frame["_sku"] != "") & frame["_sku"].duplicated(keep="first")
            if duplicated.any():
                logger.warning(
                    "Dropping %d variants whose SKU repeats an earlier variant "
                    "(sku_duplicate_strategy=drop)",
                    int(duplicated.sum()),
                )
                frame = frame[~duplicated].reset_index(drop=True)
        self._map_option_attributes(frame)

        option_cols = [c for c in frame.columns if c.startswith("_opt_")]

        frame["_variation_theme"] = ""
        frame["_parentage"] = ""
        frame["_parent_sku"] = ""
        frame["_relationship_type"] = ""
        frame["_family"] = frame["Handle"].astype(str)
        frame["_family_split"] = False

        theme_info = self._theme_info(frame, option_cols)
        product_types: dict[str, str] = (
            frame.groupby("Handle", sort=False)["_feed_product_type"].first().to_dict()
            if "_feed_product_type" in frame.columns
            else {}
        )

        theme_by_handle: dict[str, str] = {}
        for handle, parts in theme_info.items():
            combined_name = self._combined_name([p for _, p in parts])
            allowed = self._allowed_themes.get(
                str(product_types.get(handle, "")).upper()
            )
            if allowed and combined_name.upper() not in allowed and len(parts) > 1:
                for opt_col, part in parts:
                    rest = self._combined_name(
                        [q for oc, q in parts if oc != opt_col]
                    )
                    if rest.upper() in allowed:
                        mask = frame["Handle"] == handle
                        frame.loc[mask, "_family"] = (
                            handle + "//" + frame.loc[mask, opt_col].astype(str)
                        )
                        frame.loc[mask, "_family_split"] = True
                        logger.info(
                            "Splitting '%s' into one family per '%s' value: "
                            "theme %s is not allowed for %s, using %s",
                            handle,
                            opt_col.removeprefix("_opt_"),
                            combined_name,
                            product_types.get(handle, "?"),
                            rest,
                        )
                        combined_name = rest
                        break
            theme_by_handle[handle] = combined_name

        frame["_variation_theme"] = frame["Handle"].map(theme_by_handle).fillna("")
        has_theme = frame["_variation_theme"] != ""

        parent_skus = frame["_family"].map(
            self._parent_sku_map(frame.loc[has_theme, "_family"])
        )
        frame.loc[has_theme, "_parentage"] = _CHILD
        frame.loc[has_theme, "_parent_sku"] = parent_skus[has_theme]
        frame.loc[has_theme, "_relationship_type"] = self._relationship_type

        parents = self._build_parent_rows(frame[has_theme])
        combined = pd.concat([parents, frame], ignore_index=True, sort=False)
        combined = self._order_families(combined)

        logger.info(
            "Variation build: %d parents, %d children, %d standalone",
            len(parents),
            int(has_theme.sum()),
            int((combined["_parentage"] == "").sum()),
        )
        return combined

    # ------------------------------------------------------------------ #

    def _assign_skus(self, frame: pd.DataFrame) -> None:
        """Use the Shopify variant SKU, generating/deduplicating as needed.

        Shopify allows the same SKU on multiple variants; Amazon does not.
        Duplicates get a deterministic suffix built from the variant's
        option values (e.g. ``-18KYG5`` for "18K Yellow Gold" size 5), so
        the same input always produces the same output SKUs.
        """
        sku = (
            frame["Variant SKU"].astype(str).str.strip()
            if "Variant SKU" in frame.columns
            else pd.Series("", index=frame.index)
        )
        missing = sku == ""
        if missing.any():
            suffix = frame.groupby("Handle", sort=False).cumcount().add(1).astype(str)
            generated = frame["Handle"].map(lambda h: slugify(h, 34)) + "-" + suffix
            sku = sku.mask(missing, generated)
            logger.warning("Generated SKUs for %d variants without one", missing.sum())
        if self._sku_duplicate_strategy in ("suffix", "suffix_others"):
            sku = self._deduplicate_skus(
                frame, sku, keep_first=self._sku_duplicate_strategy == "suffix_others"
            )
        frame["_sku"] = sku

    def _parent_sku_map(self, families: pd.Series) -> dict[str, str]:
        """Build a unique parent SKU per variation family (max 40 chars).

        A family id is either the handle or ``handle//option value`` when a
        product was split per option. Long ids can truncate to the same
        slug; collisions get a deterministic CRC suffix so re-runs always
        produce the same SKUs.
        """
        max_slug = max(1, 40 - len(self._parent_prefix))
        mapping: dict[str, str] = {}
        owner: dict[str, str] = {}
        for family in pd.unique(families):
            sku = self._parent_prefix + slugify(str(family).replace("//", "-"), max_slug)
            if owner.get(sku, family) != family:
                code = format(zlib.crc32(str(family).encode()) & 0xFFFF, "04X")
                sku = f"{sku[: 40 - len(code) - 1]}-{code}"
            owner.setdefault(sku, family)
            mapping[family] = sku
        return mapping

    @staticmethod
    def _option_code(value: str, letters_only: bool = False) -> str:
        """Compress option text into a short SKU-safe code.

        ``"18K Yellow Gold"`` -> ``"18KYG"``; with ``letters_only`` the
        digit tokens are dropped (``"YG"``) - useful when the SKU already
        encodes karat and size and only the colour needs differentiating.
        """
        parts = []
        for token in str(value).split():
            token = "".join(ch for ch in token if ch.isalnum())
            if not token:
                continue
            if any(c.isdigit() for c in token):
                if not letters_only:
                    parts.append(token.upper())
            else:
                parts.append(token[0].upper())
        return "".join(parts)[:12]

    def _deduplicate_skus(
        self, frame: pd.DataFrame, sku: pd.Series, keep_first: bool = False
    ) -> pd.Series:
        """Make duplicated SKUs unique with option-derived suffixes.

        Only the option(s) that actually differ within a duplicate group
        contribute to the suffix, so four gold colours sharing one SKU
        become ``SKU-9KYG``, ``SKU-18KRG``, ... rather than repeating the
        size that is already part of the SKU. With ``keep_first`` the first
        variant of each group keeps the original SKU untouched and only
        the remaining duplicates are suffixed.
        """
        duplicated = (sku != "") & sku.duplicated(keep=False)
        if not duplicated.any():
            return sku

        option_cols = [
            c for c in ("Option1 Value", "Option2 Value", "Option3 Value")
            if c in frame.columns
        ]
        adjusted = sku.copy()
        for _, indices in sku[duplicated].groupby(sku[duplicated]).groups.items():
            block = frame.loc[indices, option_cols].astype(str)
            distinguishing = [
                c for c in option_cols if block[c].nunique() > 1
            ] or option_cols

            def suffixed(idx: int, letters_only: bool) -> str:
                code = self._option_code(
                    " ".join(str(frame.at[idx, c]) for c in distinguishing),
                    letters_only=letters_only,
                )
                return f"{sku[idx][: 39 - len(code)]}-{code}" if code else sku[idx]

            # Prefer the short colour-only code ("-YG"); karat and size are
            # usually already inside the SKU. Fall back to the full code
            # for rows the short one cannot disambiguate.
            group_new: dict[int, str] = {}
            for position, idx in enumerate(indices):
                if keep_first and position == 0:
                    group_new[idx] = sku[idx]
                    continue
                group_new[idx] = suffixed(idx, letters_only=True)
            seen_counts = pd.Series(list(group_new.values())).value_counts()
            for idx, candidate in group_new.items():
                if seen_counts[candidate] > 1 and not (
                    keep_first and idx == indices[0]
                ):
                    group_new[idx] = suffixed(idx, letters_only=False)
            adjusted[list(group_new)] = list(group_new.values())

        # Anything still colliding (identical options too) gets a counter.
        still = (adjusted != "") & adjusted.duplicated(keep=False)
        if still.any():
            counter = adjusted.groupby(adjusted).cumcount()
            needs_counter = still & (counter > 0)
            adjusted[needs_counter] = (
                adjusted[needs_counter].str.slice(0, 36)
                + "-"
                + (counter[needs_counter] + 1).astype(str)
            )
        logger.warning(
            "SKU deduplication: %d of %d duplicate-SKU variants renamed with "
            "option-code suffixes (%d kept their original SKU)",
            int((adjusted != sku).sum()),
            int(duplicated.sum()),
            int((adjusted[duplicated] == sku[duplicated]).sum()),
        )
        return adjusted

    def _map_option_attributes(self, frame: pd.DataFrame) -> None:
        """Copy ``_opt_*`` values into their Amazon attribute columns."""
        for target in set(self._option_attributes.values()):
            if target not in frame.columns:
                frame[target] = ""
        for opt_col in [c for c in frame.columns if c.startswith("_opt_")]:
            option_name = opt_col.removeprefix("_opt_")
            target = self._option_attributes.get(option_name)
            if not target:
                continue
            values = frame[opt_col].astype(str).str.strip()
            frame[target] = frame[target].mask(
                (frame[target] == "") & (values != ""), values
            )
        # Lukson rule: metal stamp falls back to Option1 Value when no
        # purity-like option was matched.
        if "_metal_stamp" in frame.columns and "Option1 Value" in frame.columns:
            fallback = frame["Option1 Value"].astype(str).str.strip()
            fallback = fallback.where(~fallback.str.lower().isin(["default title", ""]), "")
            frame["_metal_stamp"] = frame["_metal_stamp"].mask(
                frame["_metal_stamp"] == "", fallback
            )

    def _theme_info(
        self, frame: pd.DataFrame, option_cols: list[str]
    ) -> dict[str, list[tuple[str, str]]]:
        """For each multi-variant handle: the varying (option column, theme
        part) pairs. Options only count when they actually vary within the
        product and their name maps to a known Amazon theme part.
        """
        info: dict[str, list[tuple[str, str]]] = {}
        if not option_cols:
            return info
        grouped = frame.groupby("Handle", sort=False)
        nunique = grouped[option_cols].nunique()
        sizes = grouped.size()
        for handle, counts in nunique.iterrows():
            if sizes[handle] <= 1:
                continue
            parts: list[tuple[str, str]] = []
            for col in option_cols:
                if counts[col] <= 1:
                    continue
                part = self._themes.get(col.removeprefix("_opt_"))
                if part and all(p != part for _, p in parts):
                    parts.append((col, part))
            if parts:
                info[handle] = parts
        return info

    def _combined_name(self, names: list[str]) -> str:
        """Join theme parts into an Amazon theme name."""
        if len(names) == 1:
            return names[0]
        key = "|".join(sorted(names))
        return self._combined.get(key, "/".join(sorted(names)))

    def _build_parent_rows(self, children: pd.DataFrame) -> pd.DataFrame:
        """Synthesise one parent row per variation family.

        The parent inherits product-level data from the first child but has
        its own SKU, no parent linkage and no variant-specific values.
        """
        if children.empty:
            return children.iloc[0:0].copy()

        parents = children.drop_duplicates(subset=["_family"], keep="first").copy()
        parents["_sku"] = parents["_parent_sku"]
        parents["_parentage"] = _PARENT
        parents["_parent_sku"] = ""
        parents["_relationship_type"] = ""
        # Parents must not carry variant-specific attributes. Colour/purity
        # stay on parents of families that were split per colour - each such
        # parent represents exactly one colour.
        for col in ("_ring_size", "_weight_option"):
            if col in parents.columns:
                parents[col] = ""
        for col in ("_color", "_metal_stamp"):
            if col in parents.columns:
                parents.loc[~parents["_family_split"], col] = ""
        for col in parents.columns:
            if col.startswith("_opt_"):
                parents[col] = ""
        for col in ("Variant SKU", "Variant Price", "Variant Compare At Price",
                    "Variant Barcode", "Variant Inventory Qty", "Variant Grams"):
            if col in parents.columns:
                parents[col] = ""
        return parents

    @staticmethod
    def _order_families(frame: pd.DataFrame) -> pd.DataFrame:
        """Order rows so each parent immediately precedes its children."""
        family_order = {f: i for i, f in enumerate(frame["_family"].unique())}
        rank = frame["_family"].map(family_order)
        parent_last = np.where(frame["_parentage"] == _PARENT, 0, 1)
        frame = frame.assign(_h=rank, _p=parent_last)
        frame = frame.sort_values(["_h", "_p"], kind="stable").drop(columns=["_h", "_p"])
        return frame.reset_index(drop=True)
