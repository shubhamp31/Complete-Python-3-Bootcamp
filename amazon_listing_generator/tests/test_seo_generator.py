"""Tests for SEO content generation."""

from __future__ import annotations

import pandas as pd
import pytest

from core.seo_generator import SEOGenerator


@pytest.fixture()
def frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Title": ["Aurora Solitaire Ring"],
            "Body (HTML)": ["<p>A timeless <b>solitaire</b> ring.</p>"],
            "Tags": ["solitaire, engagement"],
            "SEO Description": [""],
            "_metal_stamp": ["14K"],
            "_product_type_label": ["Ring"],
            "_stone_type": ["Lab Grown Diamond"],
            "_stone_weight": ["0.50"],
            "_stone_clarity": ["VS1"],
        }
    )


def test_title_structure_and_limit(frame: pd.DataFrame, rules: dict, defaults: dict) -> None:
    out = SEOGenerator(rules, defaults).generate(frame)
    title = out["_seo_title"].iloc[0]
    assert len(title) <= 200
    assert title.startswith("Lukson 14K Gold")
    assert "Aurora Solitaire Ring" in title
    assert "for Women" in title


def test_five_bullets_generated(frame: pd.DataFrame, rules: dict, defaults: dict) -> None:
    out = SEOGenerator(rules, defaults).generate(frame)
    for i in range(1, 6):
        bullet = out[f"_bullet_{i}"].iloc[0]
        assert bullet, f"bullet {i} is empty"
        assert len(bullet) <= 500
    assert "14K" in out["_bullet_1"].iloc[0]
    assert "0.50 carat" in out["_bullet_2"].iloc[0]


def test_description_strips_html(frame: pd.DataFrame, rules: dict, defaults: dict) -> None:
    out = SEOGenerator(rules, defaults).generate(frame)
    description = out["_seo_description"].iloc[0]
    assert "<" not in description
    assert "solitaire" in description.lower()
    assert len(description) <= 2000


def test_search_terms_within_limit_and_no_brand(
    frame: pd.DataFrame, rules: dict, defaults: dict
) -> None:
    out = SEOGenerator(rules, defaults).generate(frame)
    terms = out["_search_terms"].iloc[0]
    assert len(terms.encode("utf-8")) <= 200
    assert "lukson" not in terms.lower()
    words = terms.split()
    assert len(words) == len(set(words)), "search terms must not repeat"


def test_banned_words_removed(rules: dict, defaults: dict) -> None:
    frame = pd.DataFrame(
        {
            "Title": ["Best Seller Free Ring"],
            "Body (HTML)": [""],
            "Tags": [""],
            "_metal_stamp": ["18K"],
            "_product_type_label": ["Ring"],
        }
    )
    title = SEOGenerator(rules, defaults).generate(frame)["_seo_title"].iloc[0]
    assert "free" not in title.lower()
