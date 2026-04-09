"""
utils.py — Shared helper utilities for the academic_ocr module.

Provides file validation, image quality assessment (Laplacian-variance
blur detection), JSON persistence, and pretty-printing.

All functions use the module-level logger ``academic_ocr.utils`` so that
consumers can configure log verbosity from their own application.
"""

import hashlib
import json
import logging
import os
from typing import Any, Dict, Set, Tuple

from PIL import Image, ImageFilter

from .exceptions import FileValidationError, ImageQualityError

# ── Module logger ─────────────────────────────────────────────────────
logger = logging.getLogger(__name__)

# ── Configuration constants ───────────────────────────────────────────

# Laplacian-variance threshold for blur detection.
# Images with a variance below this value are considered too blurry.
# Tune this up (stricter) or down (more lenient) as needed.
BLUR_THRESHOLD: float = 50.0

# Maximum image dimension (width or height) used for blur analysis.
# Larger images are downsampled to this size before the Laplacian
# filter is applied, to keep the check O(1) with respect to original
# resolution.  This does NOT affect the image sent to Gemini.
_BLUR_CHECK_MAX_DIM: int = 1024

# Maximum allowed file size in bytes (25 MB — Gemini upload limit).
MAX_FILE_SIZE_BYTES: int = 25 * 1024 * 1024

# Supported file extensions → MIME types.
_MIME_MAP: Dict[str, str] = {
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png":  "image/png",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".pdf":  "application/pdf",
}

# Extensions that can be blur-checked via Pillow.
_IMAGE_EXTENSIONS: Set[str] = {".jpg", ".jpeg", ".png", ".webp", ".heic"}

__all__ = [
    "validate_file",
    "check_image_quality",
    "compute_file_hash",
    "save_sample_output",
    "pretty_print",
    "BLUR_THRESHOLD",
    "MAX_FILE_SIZE_BYTES",
]


# ═════════════════════════════════════════════════════════════════════
#  File validation
# ═════════════════════════════════════════════════════════════════════

def validate_file(filepath: str) -> str:
    """Validate that *filepath* exists, has a supported extension, and
    does not exceed the maximum file size.

    Args:
        filepath: Absolute or relative path to the document file.

    Returns:
        The MIME type string associated with the file extension
        (e.g. ``"image/jpeg"``).

    Raises:
        FileValidationError: If the file does not exist, its extension
            is unsupported, or it exceeds ``MAX_FILE_SIZE_BYTES``.
    """
    # ── Existence check ───────────────────────────────────────────────
    if not os.path.isfile(filepath):
        raise FileValidationError(f"File not found: {filepath}")

    # ── Extension check ───────────────────────────────────────────────
    ext = os.path.splitext(filepath)[1].lower()
    mime = _MIME_MAP.get(ext)

    if mime is None:
        supported = ", ".join(sorted(_MIME_MAP.keys()))
        raise FileValidationError(
            f"Unsupported file extension '{ext}'. "
            f"Supported extensions: {supported}"
        )

    # ── Size check ────────────────────────────────────────────────────
    file_size = os.path.getsize(filepath)
    if file_size > MAX_FILE_SIZE_BYTES:
        size_mb = round(file_size / (1024 * 1024), 2)
        limit_mb = round(MAX_FILE_SIZE_BYTES / (1024 * 1024), 2)
        raise FileValidationError(
            f"File size ({size_mb} MB) exceeds the maximum allowed "
            f"size of {limit_mb} MB."
        )

    if file_size == 0:
        raise FileValidationError("File is empty (0 bytes).")

    logger.debug(
        "File validated: %s (ext=%s, mime=%s, size=%d bytes)",
        filepath, ext, mime, file_size,
    )
    return mime


# ═════════════════════════════════════════════════════════════════════
#  Image quality / blur detection
# ═════════════════════════════════════════════════════════════════════

def check_image_quality(
    filepath: str,
    threshold: float = BLUR_THRESHOLD,
) -> Tuple[bool, float]:
    """Detect whether an image is too blurry for reliable OCR.

    Uses a **Laplacian-variance** method:

    1. The image is converted to greyscale.
    2. Large images are downsampled to ``_BLUR_CHECK_MAX_DIM`` px on the
       longest side to keep the computation efficient at scale.
    3. A 3×3 Laplacian edge-detection kernel is applied.
    4. The variance of the resulting pixel values is computed.  A low
       variance indicates few detectable edges → blurry / out-of-focus.

    PDFs are **skipped** automatically because Pillow cannot rasterise
    PDF pages without external dependencies (Ghostscript / poppler).

    Args:
        filepath:  Path to the image file.
        threshold: Minimum acceptable Laplacian variance.  Default is
                   ``BLUR_THRESHOLD``.

    Returns:
        A ``(is_acceptable, variance)`` tuple.

        * **is_acceptable** — ``True`` if the image is sharp enough.
        * **variance** — The computed Laplacian variance (higher →
          sharper).  Returns ``-1.0`` for non-image files that are
          auto-accepted.

    Raises:
        ImageQualityError: If the image fails the sharpness check.
        FileValidationError: If Pillow cannot open the file.
    """
    ext = os.path.splitext(filepath)[1].lower()

    # PDFs can't be blur-checked with Pillow alone — accept automatically.
    if ext not in _IMAGE_EXTENSIONS:
        logger.debug("Skipping blur check for non-image file: %s", filepath)
        return True, -1.0

    # ── Open image ────────────────────────────────────────────────────
    try:
        img = Image.open(filepath)
        img.load()  # Force full decode to catch truncated files.
    except Exception as exc:
        raise FileValidationError(
            f"Could not open image for quality check: {exc}"
        ) from exc

    # ── Downsample for performance ────────────────────────────────────
    # We only need edge information, not full resolution.  Downsampling
    # keeps blur-check time constant regardless of input resolution.
    grey = img.convert("L")
    max_dim = max(grey.size)
    if max_dim > _BLUR_CHECK_MAX_DIM:
        scale = _BLUR_CHECK_MAX_DIM / max_dim
        new_size = (int(grey.width * scale), int(grey.height * scale))
        grey = grey.resize(new_size, Image.LANCZOS)
        logger.debug(
            "Downsampled image from %s to %s for blur check",
            img.size, new_size,
        )

    # ── Apply 3×3 Laplacian kernel ────────────────────────────────────
    #   [0,  1, 0]
    #   [1, -4, 1]
    #   [0,  1, 0]
    laplacian = grey.filter(ImageFilter.Kernel(
        size=(3, 3),
        kernel=[0, 1, 0, 1, -4, 1, 0, 1, 0],
        scale=1,
        offset=128,  # Shift to avoid negative pixel values.
    ))

    # ── Compute variance ──────────────────────────────────────────────
    pixels = list(laplacian.getdata())
    n = len(pixels)
    mean = sum(pixels) / n
    variance = sum((p - mean) ** 2 for p in pixels) / n
    variance = round(variance, 2)

    is_acceptable = variance >= threshold

    if is_acceptable:
        logger.info(
            "Image quality OK: sharpness=%.2f (threshold=%.2f) — %s",
            variance, threshold, filepath,
        )
    else:
        logger.warning(
            "Image too blurry: sharpness=%.2f (threshold=%.2f) — %s",
            variance, threshold, filepath,
        )

    return is_acceptable, variance


# ═════════════════════════════════════════════════════════════════════
#  File hashing
# ═════════════════════════════════════════════════════════════════════

_HASH_CHUNK_SIZE: int = 65_536  # 64 KB — keeps memory flat for large PDFs.


def compute_file_hash(filepath: str) -> str:
    """Compute the SHA-256 hex digest of a file.

    Reads the file in 64 KB chunks so that large PDFs do not cause
    memory spikes.  The returned hex string is used as a cache key —
    identical bytes uploaded twice produce the same hash, allowing the
    extractor to skip a duplicate Gemini call.

    Args:
        filepath: Absolute or relative path to the file.

    Returns:
        A lowercase 64-character SHA-256 hex digest string.

    Raises:
        FileValidationError: If the file cannot be read.
    """
    sha256 = hashlib.sha256()

    try:
        with open(filepath, "rb") as fh:
            while True:
                chunk = fh.read(_HASH_CHUNK_SIZE)
                if not chunk:
                    break
                sha256.update(chunk)
    except OSError as exc:
        raise FileValidationError(
            f"Could not read file for hashing: {exc}"
        ) from exc

    digest = sha256.hexdigest()
    logger.debug("File hash computed: %s → %s", filepath, digest[:16] + "…")
    return digest


# ═════════════════════════════════════════════════════════════════════
#  Output helpers
# ═════════════════════════════════════════════════════════════════════

def save_sample_output(data: dict, filename: str) -> str:
    """Persist *data* as a formatted JSON file inside ``sample_outputs/``.

    The ``sample_outputs/`` directory is created next to this source file
    if it does not already exist.

    Args:
        data:     Dictionary to serialise.
        filename: Target filename (e.g. ``"marksheet_result.json"``).
                  A ``.json`` suffix is appended automatically if missing.

    Returns:
        The absolute path of the saved JSON file.
    """
    if not filename.endswith(".json"):
        filename += ".json"

    output_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "sample_outputs",
    )
    os.makedirs(output_dir, exist_ok=True)

    output_path = os.path.join(output_dir, filename)

    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)

    logger.info("Sample output saved to: %s", output_path)
    return output_path


def pretty_print(data: dict) -> None:
    """Print *data* as indented, human-readable JSON to stdout.

    Args:
        data: Dictionary to display.
    """
    print(json.dumps(data, indent=2, ensure_ascii=False))
