"""Tests for the configurable field mapping engine."""

from __future__ import annotations

import pandas as pd
import pytest

from core.mapper import FieldMapper
from core.models import FieldMappingConfig
from core.utils import ConfigError


def make_config(mappings: list[dict]) -> FieldMappingConfig:
    return FieldMappingConfig(mappings=mappings)


@pytest.fixture()
def working() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Variant SKU": ["SKU-1", "SKU-2"],
            "_seo_title": ["Title 1", "Title 2"],
            "_metal_stamp": ["14K", "18K"],
        }
    )


def test_shopify_source(working: pd.DataFrame) -> None:
    config = make_config(
        [{"amazon_field": "item_sku", "source": {"type": "shopify", "column": "Variant SKU"}}]
    )
    out = FieldMapper(config, {}).map(working)
    assert out["item_sku"].tolist() == ["SKU-1", "SKU-2"]


def test_internal_source(working: pd.DataFrame) -> None:
    config = make_config(
        [{"amazon_field": "item_name", "source": {"type": "internal", "column": "_seo_title"}}]
    )
    out = FieldMapper(config, {}).map(working)
    assert out["item_name"].tolist() == ["Title 1", "Title 2"]


def test_constant_and_default_sources(working: pd.DataFrame) -> None:
    config = make_config(
        [
            {"amazon_field": "country_of_origin", "source": {"type": "constant", "value": "India"}},
            {"amazon_field": "brand_name", "source": {"type": "default", "key": "brand"}},
        ]
    )
    out = FieldMapper(config, {"brand": "Lukson"}).map(working)
    assert (out["country_of_origin"] == "India").all()
    assert (out["brand_name"] == "Lukson").all()


def test_template_source(working: pd.DataFrame) -> None:
    config = make_config(
        [
            {
                "amazon_field": "part_number",
                "source": {"type": "template", "template": "{Variant SKU}-{_metal_stamp}"},
            }
        ]
    )
    out = FieldMapper(config, {}).map(working)
    assert out["part_number"].tolist() == ["SKU-1-14K", "SKU-2-18K"]


def test_missing_shopify_column_yields_blank(working: pd.DataFrame) -> None:
    config = make_config(
        [{"amazon_field": "external_product_id", "source": {"type": "shopify", "column": "Nope"}}]
    )
    out = FieldMapper(config, {}).map(working)
    assert (out["external_product_id"] == "").all()


def test_invalid_rule_raises_config_error(working: pd.DataFrame) -> None:
    config = make_config(
        [{"amazon_field": "item_sku", "source": {"type": "shopify"}}]  # no column
    )
    with pytest.raises(ConfigError, match="requires a 'column'"):
        FieldMapper(config, {}).map(working)


def test_new_mapping_needs_no_code_change(working: pd.DataFrame) -> None:
    """Adding a brand-new field is purely a JSON edit."""
    config = make_config(
        [{"amazon_field": "warranty_type", "source": {"type": "constant", "value": "No Warranty"}}]
    )
    out = FieldMapper(config, {}).map(working)
    assert (out["warranty_type"] == "No Warranty").all()
