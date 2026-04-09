"""
extractor.py — Core extraction engine for the academic_ocr module.

Provides :class:`AcademicExtractor`, a fully importable, production-ready
class that wraps Google Gemini to classify and extract structured data
from academic marksheets and certificates.

Uses the **new** ``google.genai`` SDK (not the deprecated
``google.generativeai``).  The client is instance-scoped — no global
state, fully thread-safe.

Design decisions for scale:
    * **In-memory SHA-256 cache** — duplicate files skip the Gemini call
      entirely, returning cached results with ``cached: True``.
    * **Retry with exponential back-off** — transient Gemini failures
      (quota bursts, 503s) are retried up to ``MAX_RETRIES`` times.
    * **Resource cleanup** — uploaded files are deleted from Google's
      servers after extraction completes (success *or* failure).
    * **needs_review flag** — automatically flags extractions that may
      need human review (low confidence, missing fields, etc.).
    * **processing_ms** — wall-clock extraction time in milliseconds
      for latency monitoring.

Usage::

    from academic_ocr.extractor import AcademicExtractor

    extractor = AcademicExtractor(api_key="YOUR_KEY")
    result = extractor.extract("path/to/marksheet.jpg")
"""

import json
import logging
import time
from typing import Any, Dict, List, Optional

from google import genai
from google.genai import types

from .exceptions import (
    ExtractionError,
    ImageQualityError,
    ParseError,
)
from .prompt import SYSTEM_PROMPT
from .schemas import DocumentExtraction
from .utils import check_image_quality, compute_file_hash, validate_file

# ── Module logger ─────────────────────────────────────────────────────
logger = logging.getLogger(__name__)

# ── Retry configuration ──────────────────────────────────────────────
MAX_RETRIES: int = 3
INITIAL_BACKOFF_SECONDS: float = 2.0

# ── Confidence threshold for needs_review ────────────────────────────
_LOW_CONFIDENCE_THRESHOLD: float = 0.75


class AcademicExtractor:
    """Extract structured academic data from document images and PDFs.

    Uses the new ``google.genai`` SDK.  Each instance holds its own
    ``genai.Client`` — no global state, fully thread-safe.

    Args:
        api_key:   Google AI / Gemini API key.
        use_cache: If ``True`` (default), identical files (by SHA-256)
                   return cached results without a second Gemini call.
        model:     Gemini model name.  Defaults to ``gemini-2.5-flash``.
    """

    _DEFAULT_MODEL: str = "gemini-2.5-flash-lite"

    def __init__(
        self,
        api_key: str,
        use_cache: bool = True,
        model: str | None = None,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ValueError(
                "api_key must be a non-empty string.  "
                "Set GEMINI_API_KEY in your .env file or environment."
            )

        self._model_name: str = model or self._DEFAULT_MODEL
        self._client = genai.Client(api_key=api_key)
        self._use_cache: bool = use_cache
        self._cache: Dict[str, Dict[str, Any]] = {}

        logger.info(
            "AcademicExtractor initialised (model=%s, cache=%s)",
            self._model_name, use_cache,
        )

    # ================================================================== #
    #  Public API                                                         #
    # ================================================================== #

    def extract(self, filepath: str) -> Dict[str, Any]:
        """Extract structured data from an academic document.

        Pipeline:
            1. Validate the file (existence, extension, size).
            2. Check image quality — reject blurry images.
            3. Compute file hash — return cached result if available.
            4. Upload the file to Gemini.
            5. Generate a structured JSON response.
            6. Parse and reshape the response.
            7. Clean up the uploaded file.
            8. Cache the result.

        Args:
            filepath: Path to a JPG, JPEG, PNG, WEBP, HEIC image or PDF.

        Returns:
            A dict with ``kind``, ``needs_review``, ``processing_ms``,
            ``cached``, plus kind-specific fields.
        """
        start_time = time.monotonic()
        logger.info("Starting extraction: %s", filepath)

        # 1. Validate ─────────────────────────────────────────────────
        mime_type = validate_file(filepath)

        # 2. Blur check ───────────────────────────────────────────────
        is_sharp, variance = check_image_quality(filepath)
        if not is_sharp:
            raise ImageQualityError(
                f"Image is too blurry for reliable OCR "
                f"(sharpness: {variance}, minimum: 50.0).",
                sharpness_score=variance,
                threshold=50.0,
            )

        # 3. Cache check ──────────────────────────────────────────────
        file_hash = compute_file_hash(filepath)
        if self._use_cache and file_hash in self._cache:
            logger.info("Cache hit: %s", filepath)
            cached_result = self._cache[file_hash].copy()
            cached_result["cached"] = True
            cached_result["processing_ms"] = 0
            return cached_result

        # 4. Upload ───────────────────────────────────────────────────
        uploaded_file = self._upload_with_retry(filepath, mime_type)

        try:
            # 5. Generate ─────────────────────────────────────────────
            raw_text = self._generate_with_retry(uploaded_file)

            # 6. Parse + reshape ──────────────────────────────────────
            data = self._parse_response(raw_text)
            processing_ms = int((time.monotonic() - start_time) * 1000)
            result = self._reshape(data, processing_ms)

        finally:
            # 7. Cleanup ──────────────────────────────────────────────
            self._cleanup_uploaded_file(uploaded_file)

        # 8. Cache ────────────────────────────────────────────────────
        if self._use_cache:
            self._cache[file_hash] = result.copy()

        return result

    def clear_cache(self) -> int:
        """Clear the in-memory result cache."""
        count = len(self._cache)
        self._cache.clear()
        logger.info("Cache cleared (%d entries).", count)
        return count

    # ================================================================== #
    #  Upload with retry                                                  #
    # ================================================================== #

    def _upload_with_retry(self, filepath: str, mime_type: str) -> Any:
        """Upload a file to Gemini with retry + exponential back-off."""
        last_exc: Exception | None = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                logger.debug(
                    "Uploading file (attempt %d/%d): %s",
                    attempt, MAX_RETRIES, filepath,
                )
                uploaded = self._client.files.upload(
                    file=filepath,
                    config=types.UploadFileConfig(mime_type=mime_type),
                )
                logger.info("File uploaded: %s", filepath)
                return uploaded

            except Exception as exc:
                last_exc = exc
                if attempt < MAX_RETRIES:
                    wait = INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1))
                    logger.warning(
                        "Upload attempt %d failed (%s). Retrying in %.1fs…",
                        attempt, exc, wait,
                    )
                    time.sleep(wait)

        raise ExtractionError(
            f"Upload failed after {MAX_RETRIES} attempts: {last_exc}"
        ) from last_exc

    # ================================================================== #
    #  Generate with retry                                                #
    # ================================================================== #

    def _generate_with_retry(self, uploaded_file: Any) -> str:
        """Call Gemini generate_content with retry + exponential back-off."""
        last_exc: Exception | None = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                logger.debug(
                    "Generating (attempt %d/%d)…", attempt, MAX_RETRIES,
                )
                response = self._client.models.generate_content(
                    model=self._model_name,
                    contents=[
                        uploaded_file,
                        "Extract all data from this document into the JSON schema. Output ONLY the JSON values — no reasoning or commentary.",
                    ],
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM_PROMPT,
                        response_mime_type="application/json",
                        response_schema=DocumentExtraction,
                        temperature=0.1,
                    ),
                )

                raw_text = response.text

                if not raw_text or not raw_text.strip():
                    raise ParseError(
                        "Gemini returned an empty response.",
                        raw_response=raw_text,
                    )

                logger.info("Content generated successfully.")
                return raw_text

            except ParseError:
                raise

            except Exception as exc:
                last_exc = exc
                if attempt < MAX_RETRIES:
                    wait = INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1))
                    logger.warning(
                        "Generation attempt %d failed (%s). Retrying in %.1fs…",
                        attempt, exc, wait,
                    )
                    time.sleep(wait)

        raise ExtractionError(
            f"Gemini API failed after {MAX_RETRIES} attempts: {last_exc}"
        ) from last_exc

    # ================================================================== #
    #  Parse + reshape                                                    #
    # ================================================================== #

    @staticmethod
    def _parse_response(raw_text: str) -> Dict[str, Any]:
        """Parse JSON string from Gemini."""
        try:
            return json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise ParseError(
                f"JSON decode failed: {exc}", raw_response=raw_text,
            ) from exc

    # Common truncated subject names from OCR
    _SUBJECT_FIXES = {
        "PHY & HEALTH EDUCA": "PHYSICAL & HEALTH EDUCATION",
        "WORK EXP": "WORK EXPERIENCE",
        "GEN STUDIES": "GENERAL STUDIES",
    }

    @staticmethod
    def _reshape(data: Dict[str, Any], processing_ms: int) -> Dict[str, Any]:
        """Return kind-specific fields + needs_review, processing_ms, cached."""
        kind = data.get("kind", "unknown")

        if kind == "marksheet":
            
            # Post-process truncated subject names
            record = data.get("academicRecord")
            if record and isinstance(record, dict):
                for subj in record.get("subjects", []):
                    name = subj.get("subject")
                    if name:
                        # Direct match dictionary lookup
                        fixed_name = AcademicExtractor._SUBJECT_FIXES.get(name.strip())
                        if fixed_name:
                            subj["subject"] = fixed_name

            result = {
                "kind": kind,
                "title": data.get("title"),
                "exam_type": data.get("exam_type"),
                "academicRecord": record,
                "tags": data.get("tags", []),
            }
            result["needs_review"] = AcademicExtractor._compute_needs_review(kind, result)
            result["processing_ms"] = processing_ms
            result["cached"] = False
            return result

        if kind == "certificate":
            result = {
                "kind": kind,
                "title": data.get("title"),
                "recipient": data.get("recipient"),
                "achievement": data.get("achievement"),
                "date": data.get("date"),
                "tags": data.get("tags", []),
            }
            result["needs_review"] = AcademicExtractor._compute_needs_review(kind, result)
            result["processing_ms"] = processing_ms
            result["cached"] = False
            return result

        raise ExtractionError(
            "Document could not be classified. "
            "Gemini returned kind='unknown'."
        )

    # ================================================================== #
    #  needs_review                                                       #
    # ================================================================== #

    @staticmethod
    def _compute_needs_review(kind: str, data: Dict[str, Any]) -> bool:
        """Flag extraction for human review based on quality signals."""
        if kind == "unknown":
            return True

        if kind == "marksheet":
            record: Optional[Dict[str, Any]] = data.get("academicRecord")
            if record is None:
                return True
            subjects: List[Dict[str, Any]] = record.get("subjects", [])
            if len(subjects) < 2:
                return True
            if not any(record.get(f) is not None for f in ("percentage", "sgpa", "cgpa")):
                return True
            for subj in subjects:
                conf = subj.get("confidence")
                if conf is not None and conf < _LOW_CONFIDENCE_THRESHOLD:
                    return True
            return False

        if kind == "certificate":
            if data.get("recipient") is None:
                return True
            if data.get("achievement") is None:
                return True
            return False

        return True

    # ================================================================== #
    #  Cleanup                                                            #
    # ================================================================== #

    def _cleanup_uploaded_file(self, uploaded_file: Any) -> None:
        """Delete uploaded file from Google's servers (best-effort)."""
        try:
            self._client.files.delete(name=uploaded_file.name)
            logger.debug("Uploaded file cleaned up.")
        except Exception as exc:
            logger.warning("Cleanup failed: %s", exc)
