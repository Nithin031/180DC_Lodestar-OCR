"""
main.py — CLI runner for the academic_ocr extraction module.

Usage::

    python -m academic_ocr.main path/to/document.jpg
    python -m academic_ocr.main path/to/marksheet.pdf

Loads ``GEMINI_API_KEY`` from a ``.env`` file (or the OS environment)
and runs the full extraction pipeline.  Results are pretty-printed to
the console and persisted as JSON in ``sample_outputs/``.

Exit codes:
    0 — Success.
    1 — Configuration error (missing API key, bad arguments).
    2 — Extraction error (file invalid, blurry image, API failure).
"""

import logging
import os
import sys

from dotenv import load_dotenv

from .exceptions import AcademicOCRError
from .extractor import AcademicExtractor
from .utils import pretty_print, save_sample_output


def _configure_logging() -> None:
    """Set up structured console logging for the entire module.

    Log level is controlled by the ``LOG_LEVEL`` environment variable
    (default: ``INFO``).  Set to ``DEBUG`` for verbose diagnostics.
    """
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main() -> None:
    """Entry-point for the CLI demo.

    Reads the file path from ``sys.argv[1]``, performs extraction,
    pretty-prints the result, and persists it as a JSON sample output.
    """
    # ── Bootstrap ─────────────────────────────────────────────────────
    load_dotenv()
    _configure_logging()

    logger = logging.getLogger(__name__)

    # ── Validate environment ──────────────────────────────────────────
    api_key = os.getenv("GEMINI_API_KEY")

    if not api_key:
        logger.error(
            "GEMINI_API_KEY not found.  "
            "Create a .env file with GEMINI_API_KEY=<your-key> or "
            "set it as an environment variable."
        )
        sys.exit(1)

    # ── Parse CLI arguments ───────────────────────────────────────────
    if len(sys.argv) < 2:
        print(
            "Usage: python -m academic_ocr.main <filepath>\n"
            "\n"
            "Supported formats: .jpg, .jpeg, .png, .webp, .heic, .pdf\n"
            "\n"
            "Example:\n"
            "  python -m academic_ocr.main marksheet.jpg",
            file=sys.stderr,
        )
        sys.exit(1)

    filepath = sys.argv[1]

    # ── Run extraction ────────────────────────────────────────────────
    print(f"\n[Processing]: {filepath}")
    print("-" * 55)

    extractor = AcademicExtractor(api_key=api_key)

    try:
        result = extractor.extract(filepath)
    except AcademicOCRError as exc:
        logger.error("Extraction failed: %s", exc)
        print(f"\n[Error] Extraction failed: {exc}", file=sys.stderr)
        sys.exit(2)
    except Exception as exc:
        logger.exception("Unexpected error during extraction.")
        print(f"\n[Error] Unexpected error: {exc}", file=sys.stderr)
        sys.exit(2)

    # ── Display result ────────────────────────────────────────────────
    print("\n[Success] Extraction successful!\n")
    pretty_print(result)

    # ── Persist to sample_outputs/ ────────────────────────────────────
    basename = os.path.splitext(os.path.basename(filepath))[0]
    output_filename = f"{basename}_result.json"
    saved_path = save_sample_output(result, output_filename)

    print(f"\n[Saved] Output saved to: {saved_path}\n")


if __name__ == "__main__":
    main()
