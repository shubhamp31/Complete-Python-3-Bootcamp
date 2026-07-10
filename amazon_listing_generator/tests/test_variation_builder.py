"""Tests for parent/child variation building."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from core.shopify_reader import ShopifyReader
from core.variation_builder import VariationBuilder


@pytest.fixture()
def listings(sample_csv: Path, rules: dict) -> pd.DataFrame:
    variants = ShopifyReader(sample_csv).read().variants
    return VariationBuilder(rules).build(variants)


def test_parents_created_for_multi_variant_products(listings: pd.DataFrame) -> None:
    parents = listings[listings["_parentage"] == "parent"]
    assert set(parents["Handle"]) == {"aurora-solitaire-ring", "celeste-pendant"}
    assert (parents["_sku"].str.startswith("P-")).all()


def test_children_linked_to_their_parent(listings: pd.DataFrame) -> None:
    ring_children = listings[
        (listings["Handle"] == "aurora-solitaire-ring")
        & (listings["_parentage"] == "child")
    ]
    assert len(ring_children) == 4
    parent_sku = listings[
        (listings["Handle"] == "aurora-solitaire-ring")
        & (listings["_parentage"] == "parent")
    ]["_sku"].iloc[0]
    assert (ring_children["_parent_sku"] == parent_sku).all()
    assert (ring_children["_relationship_type"] == "Variation").all()


def test_combined_variation_theme(listings: pd.DataFrame) -> None:
    ring = listings[listings["Handle"] == "aurora-solitaire-ring"]
    assert set(ring["_variation_theme"]) == {"METAL_TYPE/RING_SIZE"}


def test_single_option_theme(listings: pd.DataFrame) -> None:
    pendant = listings[listings["Handle"] == "celeste-pendant"]
    assert set(pendant["_variation_theme"]) == {"METAL_TYPE"}


def test_standalone_products_have_no_parentage(listings: pd.DataFrame) -> None:
    stud = listings[listings["Handle"] == "luna-stud-earrings"]
    assert (stud["_parentage"] == "").all()
    assert (stud["_parent_sku"] == "").all()


def test_parent_precedes_children(listings: pd.DataFrame) -> None:
    ring_rows = listings[listings["Handle"] == "aurora-solitaire-ring"]
    assert ring_rows.iloc[0]["_parentage"] == "parent"
    assert (ring_rows.iloc[1:]["_parentage"] == "child").all()


def test_metal_stamp_from_option(listings: pd.DataFrame) -> None:
    children = listings[
        (listings["Handle"] == "aurora-solitaire-ring")
        & (listings["_parentage"] == "child")
    ]
    assert set(children["_metal_stamp"]) == {"14K", "18K"}


def test_missing_sku_generated(rules: dict) -> None:
    frame = pd.DataFrame(
        {
            "Handle": ["x-ring", "x-ring"],
            "Variant SKU": ["", ""],
            "Option1 Name": ["Size", "Size"],
            "Option1 Value": ["6", "7"],
            "_opt_size": ["6", "7"],
        }
    )
    built = VariationBuilder(rules).build(frame)
    skus = built[built["_parentage"] != "parent"]["_sku"]
    assert (skus != "").all()
    assert skus.is_unique
