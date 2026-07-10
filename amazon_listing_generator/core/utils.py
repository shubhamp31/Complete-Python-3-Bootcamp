"""Shared utilities: configuration loading, logging setup and custom exceptions.

Nothing in this module depends on any other project module, so it is safe to
import from anywhere without creating circular imports.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

APP_NAME = "Amazon India Listing Generator"
LOGGER_NAME = "listing_generator"


class ListingGeneratorError(Exception):
    """Base class for all expected application errors.

    Errors of this type carry a user-friendly message that the UI can show
    directly, while the full stack trace goes to the log file.
    """


class ConfigError(ListingGeneratorError):
    """A configuration file is missing or malformed."""


class InputFileError(ListingGeneratorError):
    """A user-supplied input file cannot be read or is invalid."""


class TemplateError(ListingGeneratorError):
    """The Amazon template workbook cannot be parsed."""


def project_root() -> Path:
    """Return the application root directory.

    Works both when running from source and when frozen by PyInstaller
    (where resources are unpacked next to the executable / into _MEIPASS).
    """
    if getattr(sys, "frozen", False):  # PyInstaller bundle
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def config_dir() -> Path:
    """Return the directory that holds the JSON configuration files."""
    return project_root() / "config"


def logs_dir() -> Path:
    """Return (and create if needed) the log directory."""
    path = project_root() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Configure the application logger with console + rotating file output.

    Safe to call multiple times; handlers are only attached once.

    Args:
        level: Minimum level for the console handler. The file handler
            always records DEBUG so stack traces are never lost.

    Returns:
        The configured application logger.
    """
    logger = logging.getLogger(LOGGER_NAME)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    file_handler = logging.handlers.RotatingFileHandler(
        logs_dir() / "listing_generator.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s.%(module)s:%(lineno)d | %(message)s"
        )
    )

    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(logging.Formatter("%(levelname)-8s %(message)s"))

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a child logger of the application logger."""
    base = logging.getLogger(LOGGER_NAME)
    return base.getChild(name) if name else base


def load_json_config(name: str, directory: Path | None = None) -> dict[str, Any]:
    """Load a JSON configuration file from the config directory.

    Args:
        name: File name, e.g. ``"defaults.json"``.
        directory: Override directory (used by tests).

    Returns:
        Parsed JSON as a dictionary.

    Raises:
        ConfigError: If the file is missing or contains invalid JSON.
    """
    path = (directory or config_dir()) / name
    if not path.is_file():
        raise ConfigError(f"Configuration file not found: {path}")
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Top-level JSON in {path} must be an object")
    return data


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def strip_html(text: str) -> str:
    """Remove HTML tags and collapse whitespace from a string."""
    if not text:
        return ""
    text = _HTML_TAG_RE.sub(" ", str(text))
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    return _WHITESPACE_RE.sub(" ", text).strip()


def normalise_header(header: str) -> str:
    """Normalise a template header for tolerant matching.

    ``"Seller SKU"``, ``"seller-sku"`` and ``"item_sku"`` should all be
    comparable, so lowercase and strip every non-alphanumeric character.
    """
    return re.sub(r"[^a-z0-9]", "", str(header).lower())


def truncate_at_word(text: str, max_length: int) -> str:
    """Truncate ``text`` to ``max_length`` characters at a word boundary."""
    if len(text) <= max_length:
        return text
    cut = text[: max_length + 1]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut[:max_length].rstrip(" ,;|-")


def slugify(value: str, max_length: int = 40) -> str:
    """Convert an arbitrary string into a safe SKU fragment."""
    value = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode()
    value = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").upper()
    return value[:max_length]


def timestamp_slug() -> str:
    """Return a filesystem-safe timestamp, e.g. ``20260710_142501``."""
    return datetime.now().strftime("%Y%m%d_%H%M%S")
