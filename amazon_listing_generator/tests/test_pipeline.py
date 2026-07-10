"""End-to-end pipeline test: sample Shopify CSV -> populated template + reports."""

from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import load_workbook

from core.amazon_template import AmazonTemplate
from core.marketplace import available_marketplaces, get_generator
from core.models import GenerationRequest
from core.pipeline import AmazonIndiaPipeline  # noqa: F401 - registers generator


@pytest.fixture(scope="module")
def result_and_dir(sample_csv: Path, sample_template: Path, tmp_path_factory):
    out_dir = tmp_path_factory.mktemp("outputs")
    request = GenerationRequest(
        shopify_csv=sample_csv,
        amazon_template=sample_template,
        output_dir=out_dir,
    )
    result = get_generator("amazon_in").generate(request)
    return result, out_dir


def test_registry_exposes_amazon_india() -> None:
    assert available_marketplaces()["amazon_in"] == "Amazon India"


def test_all_outputs_created(result_and_dir) -> None:
    result, _ = result_and_dir
    for path in (
        result.output_file,
        result.validation_report,
        result.error_report,
        result.summary_report,
    ):
        assert path is not None and path.is_file(), path


def test_stats_match_sample_data(result_and_dir) -> None:
    result, _ = result_and_dir
    stats = result.stats
    assert stats.total_products == 4
    assert stats.parents == 2          # aurora ring + celeste pendant
    assert stats.children == 6         # 4 ring + 2 pendant variants
    assert stats.standalone == 2       # luna studs + nova bangle
    assert stats.total_listings == 10
    # nova-bangle has no image, no price, no SKU -> errors expected
    assert result.status == "completed_with_errors"
    assert stats.errors > 0


def test_upload_file_contents(result_and_dir) -> None:
    result, _ = result_and_dir
    layout = AmazonTemplate(result.output_file).layout()
    sheet = load_workbook(result.output_file)[layout.sheet_name]

    def column(field: str) -> list:
        col = layout.column_for(field)
        return [
            sheet.cell(row=r, column=col).value
            for r in range(layout.data_start_row, layout.data_start_row + 10)
        ]

    skus = column("item_sku")
    assert "LKS-AUR-14K-6" in skus
    assert any(str(s).startswith("P-AURORA") for s in skus)
    assert set(column("brand_name")) == {"Lukson"}
    assert set(column("country_of_origin")) == {"India"}
    assert set(column("metal_type")) == {"Gold"}
    assert set(column("stone_creation_method")) == {"Lab Grown"}

    # Parent rows carry no price; children do.
    parentage = column("parent_child")
    prices = column("standard_price")
    for p, price in zip(parentage, prices):
        if p == "parent":
            assert price is None
    titles = column("item_name")
    assert all(t and len(str(t)) <= 200 for t in titles)


def test_error_report_flags_bangle(result_and_dir) -> None:
    result, _ = result_and_dir
    sheet = load_workbook(result.error_report).active
    messages = " ".join(
        str(row[4].value) for row in sheet.iter_rows(min_row=2) if row[4].value
    )
    assert "Missing" in messages


def test_performance_smoke(result_and_dir) -> None:
    """The 10-listing sample must complete in well under a second of CPU-bound
    pipeline time; the 5-minute / 20k-row budget is enforced proportionally."""
    result, _ = result_and_dir
    assert result.stats.elapsed_seconds < 30
