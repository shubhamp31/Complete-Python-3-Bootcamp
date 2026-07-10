"""Tests for image slot mapping."""

from __future__ import annotations

import pandas as pd

from core.image_mapper import ImageMapper


def test_main_and_additional_images_in_order() -> None:
    listings = pd.DataFrame({"Handle": ["ring"], "Variant Image": [""]})
    images = pd.DataFrame(
        {
            "Handle": ["ring"] * 3,
            "Image Src": [f"https://cdn.x.com/{i}.jpg" for i in (1, 2, 3)],
            "Image Position": [1, 2, 3],
        }
    )
    out = ImageMapper().apply(listings, images)
    assert out["_main_image_url"].iloc[0].endswith("1.jpg")
    assert out["_other_image_url_1"].iloc[0].endswith("2.jpg")
    assert out["_other_image_url_2"].iloc[0].endswith("3.jpg")
    assert out["_other_image_url_3"].iloc[0] == ""


def test_variant_image_becomes_main() -> None:
    listings = pd.DataFrame(
        {"Handle": ["ring"], "Variant Image": ["https://cdn.x.com/2.jpg"]}
    )
    images = pd.DataFrame(
        {
            "Handle": ["ring"] * 2,
            "Image Src": ["https://cdn.x.com/1.jpg", "https://cdn.x.com/2.jpg"],
            "Image Position": [1, 2],
        }
    )
    out = ImageMapper().apply(listings, images)
    assert out["_main_image_url"].iloc[0].endswith("2.jpg")
    # The main image must not repeat in the additional slots.
    assert out["_other_image_url_1"].iloc[0].endswith("1.jpg")
    assert out["_other_image_url_2"].iloc[0] == ""


def test_additional_images_capped() -> None:
    listings = pd.DataFrame({"Handle": ["ring"], "Variant Image": [""]})
    images = pd.DataFrame(
        {
            "Handle": ["ring"] * 12,
            "Image Src": [f"https://cdn.x.com/{i}.jpg" for i in range(12)],
            "Image Position": list(range(12)),
        }
    )
    out = ImageMapper(max_additional=8).apply(listings, images)
    assert out["_other_image_url_8"].iloc[0] != ""
    assert "_other_image_url_9" not in out.columns


def test_product_without_images() -> None:
    listings = pd.DataFrame({"Handle": ["bare"], "Variant Image": [""]})
    images = pd.DataFrame(columns=["Handle", "Image Src", "Image Position"])
    out = ImageMapper().apply(listings, images)
    assert out["_main_image_url"].iloc[0] == ""
