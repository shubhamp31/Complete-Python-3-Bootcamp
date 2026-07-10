"""Tests for the Shopify CSV reader."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.shopify_reader import ShopifyReader
from core.utils import InputFileError


def test_reads_sample_export(sample_csv: Path) -> None:
    data = ShopifyReader(sample_csv).read()
    # 4 ring variants + 1 stud + 2 pendants + 1 bangle = 8 sellable variants
    assert len(data.variants) == 8
    assert data.product_count == 4


def test_product_fields_forward_filled(sample_csv: Path) -> None:
    variants = ShopifyReader(sample_csv).read().variants
    ring = variants[variants["Handle"] == "aurora-solitaire-ring"]
    assert (ring["Title"] == "Aurora Solitaire Ring").all()
    assert (ring["Vendor"] == "Lukson").all()


def test_options_pivoted(sample_csv: Path) -> None:
    variants = ShopifyReader(sample_csv).read().variants
    ring = variants[variants["Handle"] == "aurora-solitaire-ring"]
    assert set(ring["_opt_metal purity"]) == {"14K", "18K"}
    assert set(ring["_opt_ring size"]) == {"6", "7"}
    # "Default Title" placeholder options must not produce a column value
    stud = variants[variants["Handle"] == "luna-stud-earrings"]
    assert "_opt_title" not in stud.columns or (stud.get("_opt_title", "") == "").all()


def test_images_extracted_in_order(sample_csv: Path) -> None:
    images = ShopifyReader(sample_csv).read().images
    celeste = images[images["Handle"] == "celeste-pendant"]["Image Src"].tolist()
    assert len(celeste) == 3
    assert celeste[0].endswith("celeste-1.jpg")
    assert celeste[-1].endswith("celeste-3.jpg")


def test_image_only_rows_are_not_variants(sample_csv: Path) -> None:
    variants = ShopifyReader(sample_csv).read().variants
    celeste = variants[variants["Handle"] == "celeste-pendant"]
    assert len(celeste) == 2  # the third (image-only) row is excluded


def test_missing_file_raises_friendly_error(tmp_path: Path) -> None:
    with pytest.raises(InputFileError, match="not found"):
        ShopifyReader(tmp_path / "nope.csv").read()


def test_non_shopify_csv_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text("a,b,c\n1,2,3\n", encoding="utf-8")
    with pytest.raises(InputFileError, match="Shopify"):
        ShopifyReader(bad).read()
