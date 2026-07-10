"""Amazon India Listing Generator — application entry point.

Usage:
    python app.py                       # launch the desktop UI
    python app.py --cli SHOPIFY_CSV AMAZON_TEMPLATE OUTPUT_DIR
                                        # headless run (automation / CI)
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

# Ensure the project root is importable both from source and when frozen.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.utils import ListingGeneratorError, get_logger, setup_logging  # noqa: E402

logger = get_logger("app")


def _run_cli(shopify_csv: str, template: str, output_dir: str, marketplace: str) -> int:
    """Headless pipeline run. Returns a process exit code."""
    import core.pipeline  # noqa: F401 - registers the amazon_in generator
    from core.marketplace import get_generator
    from core.models import GenerationRequest

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    request = GenerationRequest(
        shopify_csv=Path(shopify_csv),
        amazon_template=Path(template),
        output_dir=out,
    )
    result = get_generator(marketplace).generate(
        request, progress=lambda f, m: print(f"[{f * 100:5.1f}%] {m}")
    )
    print(result.message)
    print(f"Upload file:        {result.output_file}")
    print(f"Validation report:  {result.validation_report}")
    print(f"Error report:       {result.error_report}")
    print(f"Summary report:     {result.summary_report}")
    return 0 if result.status == "success" else 2


def _run_ui(marketplace: str) -> int:
    """Launch the CustomTkinter desktop application."""
    import core.pipeline  # noqa: F401 - registers the amazon_in generator
    from ui.main_window import run

    run(marketplace)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, set up logging, and dispatch UI or CLI mode."""
    parser = argparse.ArgumentParser(
        description="Convert Shopify exports into Amazon India bulk listing files."
    )
    parser.add_argument(
        "--cli",
        nargs=3,
        metavar=("SHOPIFY_CSV", "AMAZON_TEMPLATE", "OUTPUT_DIR"),
        help="run without the UI",
    )
    parser.add_argument(
        "--marketplace",
        default="amazon_in",
        help="marketplace generator to use (default: amazon_in)",
    )
    args = parser.parse_args(argv)

    setup_logging()
    try:
        if args.cli:
            return _run_cli(*args.cli, marketplace=args.marketplace)
        return _run_ui(args.marketplace)
    except ListingGeneratorError as exc:
        logger.error("%s", exc, exc_info=True)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - final safety net: never crash silently
        logger.critical("Fatal error:\n%s", traceback.format_exc())
        print(
            "FATAL: an unexpected error occurred. See the logs/ folder for details.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
