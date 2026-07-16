"""Amazon-compliant SEO content generation.

Generates titles, bullet points, descriptions and backend search terms for
every listing row, driven entirely by the templates and limits configured
in ``config/amazon_rules.json``.
"""

from __future__ import annotations

import re
from typing import Any

import pandas as pd

from core.utils import get_logger, strip_html, truncate_at_word

logger = get_logger("seo_generator")

_MULTI_SPACE = re.compile(r"\s+")
_WORD_RE = re.compile(r"[a-z0-9]+(?:[a-z0-9'-]*[a-z0-9])?")


class SEOGenerator:
    """Produces titles, bullets, descriptions and search terms."""

    def __init__(self, rules: dict[str, Any], defaults: dict[str, Any]) -> None:
        self._title_rules: dict[str, Any] = rules.get("title", {})
        self._bullet_rules: dict[str, Any] = rules.get("bullets", {})
        self._description_rules: dict[str, Any] = rules.get("description", {})
        self._search_rules: dict[str, Any] = rules.get("search_terms", {})
        self._defaults = defaults

    # ------------------------------------------------------------------ #

    def generate(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Add ``_seo_title``, ``_bullet_1..5``, ``_seo_description`` and
        ``_search_terms`` columns to the listing table.

        Args:
            frame: Listing rows enriched with ``_metal_stamp``,
                ``_product_type_label`` and stone attribute columns.

        Returns:
            The same DataFrame (mutated copy) with SEO columns added.
        """
        frame = frame.copy()
        contexts = self._build_contexts(frame)

        frame["_seo_title"] = [self._render_title(ctx) for ctx in contexts]
        bullet_templates = self._bullet_rules.get("templates", [])
        count = int(self._bullet_rules.get("count", 5))
        max_bullet = int(self._bullet_rules.get("max_length", 500))
        for i in range(count):
            template = bullet_templates[i] if i < len(bullet_templates) else ""
            frame[f"_bullet_{i + 1}"] = [
                truncate_at_word(self._safe_format(template, ctx), max_bullet)
                for ctx in contexts
            ]
        frame["_seo_description"] = [
            self._render_description(ctx) for ctx in contexts
        ]
        frame["_search_terms"] = [self._render_search_terms(ctx) for ctx in contexts]

        logger.info("Generated SEO content for %d rows", len(frame))
        return frame

    # ------------------------------------------------------------------ #

    def _build_contexts(self, frame: pd.DataFrame) -> list[dict[str, str]]:
        """Assemble one substitution context per row (vectorized column prep)."""

        def col(name: str) -> pd.Series:
            if name in frame.columns:
                return frame[name].astype(str).str.strip()
            return pd.Series([""] * len(frame), index=frame.index)

        brand = str(self._defaults.get("brand", ""))
        default_metal = str(self._defaults.get("metal_type", "Gold"))
        stone_method = str(self._defaults.get("stone_creation_method", "Lab Grown"))
        audience = str(self._title_rules.get("audience_default", "Women"))
        strip_phrases = [
            p for p in self._title_rules.get("strip_phrases", []) if p.strip()
        ]

        purity = col("_metal_stamp")
        metal_type = col("_metal_type").replace("", default_metal)
        stone_type = col("_stone_type").replace("", self._defaults.get("stone_type", "Diamond"))
        product_label = col("_product_type_label").replace("", "Jewellery")
        titles = col("Title")
        bodies = col("Body (HTML)").map(strip_html)
        seo_desc = col("SEO Description")
        tags = col("Tags")
        stone_weight = col("_stone_weight")
        stone_clarity = col("_stone_clarity")

        contexts: list[dict[str, str]] = []
        for i in frame.index:
            # Avoid "Lab Grown Lab Grown Diamond" when the stone type
            # already names the creation method.
            if stone_method.lower() in stone_type[i].lower():
                stone_phrase = stone_type[i]
            else:
                stone_phrase = _MULTI_SPACE.sub(
                    " ", f"{stone_method} {stone_type[i]}"
                ).strip()
            name = titles[i]
            for phrase in strip_phrases:
                name = re.sub(re.escape(phrase), "", name, flags=re.IGNORECASE)
            name = _MULTI_SPACE.sub(" ", name).replace(" - ", " ").strip(" -|")
            detail_bits = []
            if stone_weight[i]:
                detail_bits.append(f"{stone_weight[i]} carat")
            detail_bits.append(stone_phrase)
            if stone_clarity[i]:
                detail_bits.append(f"({stone_clarity[i]} clarity)")
            contexts.append(
                {
                    "brand": brand,
                    "purity": purity[i],
                    "metal_type": metal_type[i],
                    "stone_phrase": stone_phrase,
                    "stone_type": stone_type[i],
                    "stone_detail": " ".join(detail_bits),
                    "stone_creation_method": stone_method,
                    "product_type": product_label[i],
                    "product_type_lower": product_label[i].lower(),
                    "product_title": name,
                    "collection_phrase": "",
                    "audience": audience,
                    "body": seo_desc[i] or bodies[i],
                    "tags": tags[i],
                }
            )
        return contexts

    def _render_title(self, ctx: dict[str, str]) -> str:
        """Build the item name, preferring the product's own display name."""
        max_len = int(self._title_rules.get("max_length", 200))
        name = ctx["product_title"] or ctx["product_type"]
        parts = [
            ctx["brand"],
            ctx["purity"],
            ctx["metal_type"],
            ctx["stone_phrase"],
            name,
            f"for {ctx['audience']}",
            "| Fine Jewellery",
        ]
        title = self._dedupe_words(" ".join(p for p in parts if p))
        title = self._remove_banned(title)
        return truncate_at_word(title, max_len)

    def _render_description(self, ctx: dict[str, str]) -> str:
        max_len = int(self._description_rules.get("max_length", 2000))
        template = self._description_rules.get("template", "{title}. {body}")
        body = ctx["body"] or self._description_rules.get("fallback_body", "")
        rendered = self._safe_format(
            template, {**ctx, "title": self._render_title(ctx), "body": body}
        )
        # Amazon rejects descriptions containing policy phrases such as
        # "free returns" - strip any configured banned phrase.
        for phrase in self._description_rules.get("banned_phrases", []):
            rendered = re.sub(re.escape(phrase), "", rendered, flags=re.IGNORECASE)
        return truncate_at_word(_MULTI_SPACE.sub(" ", rendered).strip(), max_len)

    def _render_search_terms(self, ctx: dict[str, str]) -> str:
        """Backend keywords: unique, lowercase, brand-free, byte-limited."""
        max_bytes = int(self._search_rules.get("max_bytes", 200))
        separator = str(self._search_rules.get("separator", " "))
        exclude = {w.lower() for w in self._search_rules.get("exclude_words", [])}
        exclude.add(ctx["brand"].lower())

        candidates: list[str] = list(self._search_rules.get("base_keywords", []))
        candidates.extend(
            [
                f"{ctx['purity'].lower()} {ctx['metal_type'].lower()}".strip(),
                ctx["product_type_lower"],
                f"{ctx['stone_creation_method'].lower()} {ctx['stone_type'].lower()}",
            ]
        )
        candidates.extend(t.strip().lower() for t in ctx["tags"].split(",") if t.strip())

        seen: set[str] = set()
        words: list[str] = []
        for phrase in candidates:
            for word in _WORD_RE.findall(phrase.lower()):
                if word in seen or word in exclude:
                    continue
                seen.add(word)
                words.append(word)

        result = ""
        for word in words:
            trial = f"{result}{separator}{word}" if result else word
            if len(trial.encode("utf-8")) > max_bytes:
                break
            result = trial
        return result

    # ------------------------------------------------------------------ #

    def _remove_banned(self, text: str) -> str:
        for banned in self._title_rules.get("banned_words", []):
            text = re.sub(rf"\b{re.escape(banned)}\b", "", text, flags=re.IGNORECASE)
        return _MULTI_SPACE.sub(" ", text).strip()

    @staticmethod
    def _dedupe_words(text: str) -> str:
        """Drop consecutive duplicate words (e.g. 'Gold Gold Ring')."""
        out: list[str] = []
        for word in text.split():
            if out and out[-1].lower() == word.lower():
                continue
            out.append(word)
        return " ".join(out)

    @staticmethod
    def _safe_format(template: str, ctx: dict[str, str]) -> str:
        """``str.format`` that tolerates unknown placeholders."""

        class _Safe(dict):
            def __missing__(self, key: str) -> str:
                return ""

        try:
            return template.format_map(_Safe(ctx))
        except (ValueError, IndexError):
            return template
