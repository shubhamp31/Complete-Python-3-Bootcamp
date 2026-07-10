"""Shared pytest fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(scope="session")
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture(scope="session")
def sample_csv(project_root: Path) -> Path:
    return project_root / "sample_data" / "sample_shopify_export.csv"


@pytest.fixture(scope="session")
def sample_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the mock Amazon template once per test session."""
    from sample_data.make_sample_template import build_template

    return build_template(tmp_path_factory.mktemp("template") / "Amazon_Template.xlsx")


@pytest.fixture(scope="session")
def rules(project_root: Path) -> dict:
    from core.utils import load_json_config

    return load_json_config("amazon_rules.json", project_root / "config")


@pytest.fixture(scope="session")
def defaults(project_root: Path) -> dict:
    from core.utils import load_json_config

    return load_json_config("defaults.json", project_root / "config")
