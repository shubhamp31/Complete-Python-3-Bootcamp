"""Tests for the validation engine."""

from __future__ import annotations

import pandas as pd
import pytest

from core.validator import ListingValidator


@pytest.fixture()
def validator(rules: dict) -> ListingValidator:
    return ListingValidator(rules)


def make_frames(**overrides: list) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Two aligned rows: a healthy child and a healthy parent."""
    amazon = {
        "item_sku": ["P-RING", "SKU-1"],
        "item_name": ["Ring", "Ring 14K"],
        "brand_name": ["Lukson", "Lukson"],
        "feed_product_type": ["ring", "ring"],
        "update_delete": ["Update", "Update"],
        "standard_price": ["", "24999"],
        "quantity": ["", "5"],
        "main_image_url": ["https://cdn.x.com/a.jpg", "https://cdn.x.com/a.jpg"],
        "product_description": ["desc", "desc"],
        "metal_stamp": ["", "14K"],
        "item_weight": ["", "2.8"],
        "parent_sku": ["", "P-RING"],
        "relationship_type": ["", "Variation"],
        "variation_theme": ["MetalStamp", "MetalStamp"],
        "stone_type": ["", "Diamond"],
        "stone_weight": ["", "0.5"],
    }
    amazon.update(overrides)
    working = pd.DataFrame({"_parentage": ["parent", "child"]})
    return pd.DataFrame(amazon), working


def errors_for(issues: pd.DataFrame, message_part: str) -> pd.DataFrame:
    return issues[issues["Message"].str.contains(message_part, case=False)]


def test_healthy_rows_pass(validator: ListingValidator) -> None:
    amazon, working = make_frames()
    issues = validator.validate(amazon, working)
    assert (issues["Severity"] != "Error").all(), issues


def test_missing_sku(validator: ListingValidator) -> None:
    amazon, working = make_frames(item_sku=["P-RING", ""])
    issues = validator.validate(amazon, working)
    assert len(errors_for(issues, "Missing SKU")) == 1


def test_duplicate_sku(validator: ListingValidator) -> None:
    amazon, working = make_frames(item_sku=["DUP", "DUP"], parent_sku=["", "DUP"])
    issues = validator.validate(amazon, working)
    assert len(errors_for(issues, "Duplicate SKU")) == 2


def test_missing_price_flagged_only_for_sellable(validator: ListingValidator) -> None:
    amazon, working = make_frames(standard_price=["", ""])
    issues = validator.validate(amazon, working)
    missing_price = errors_for(issues, "Missing 'standard_price'")
    assert len(missing_price) == 1
    assert missing_price["Row"].iloc[0] == 2  # the child, not the parent


def test_invalid_image_url(validator: ListingValidator) -> None:
    amazon, working = make_frames(
        main_image_url=["https://cdn.x.com/a.jpg", "ftp://bad/a.bmp"]
    )
    issues = validator.validate(amazon, working)
    assert len(errors_for(issues, "Invalid image URL")) == 1


def test_missing_image_error(validator: ListingValidator) -> None:
    amazon, working = make_frames(main_image_url=["https://cdn.x.com/a.jpg", ""])
    issues = validator.validate(amazon, working)
    assert len(errors_for(issues, "Missing main image")) == 1


def test_missing_purity(validator: ListingValidator) -> None:
    amazon, working = make_frames(metal_stamp=["", ""])
    issues = validator.validate(amazon, working)
    assert len(errors_for(issues, "metal purity")) == 1


def test_orphan_child_and_unknown_parent(validator: ListingValidator) -> None:
    amazon, working = make_frames(parent_sku=["", "GHOST"])
    issues = validator.validate(amazon, working)
    assert len(errors_for(issues, "parent SKU does not exist")) == 1
    # And the parent now has no children pointing at it.
    assert len(errors_for(issues, "parent has no child")) == 1


def test_missing_description_is_warning(validator: ListingValidator) -> None:
    amazon, working = make_frames(product_description=["", ""])
    issues = validator.validate(amazon, working)
    findings = errors_for(issues, "Missing product description")
    assert len(findings) == 2
    assert (findings["Severity"] == "Warning").all()


def test_row_numbers_are_one_based(validator: ListingValidator) -> None:
    amazon, working = make_frames(item_sku=["", "SKU-1"], parent_sku=["", ""])
    issues = validator.validate(amazon, working)
    assert errors_for(issues, "Missing SKU")["Row"].iloc[0] == 1
