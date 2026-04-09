"""
academic_ocr — Structured OCR extraction for academic marksheets and certificates.

This package provides a production-ready extraction pipeline powered by
Google Gemini.  Drop :class:`AcademicExtractor` into any Python backend
to extract structured academic data from document images and PDFs.

Quick start::

    from academic_ocr import AcademicExtractor

    extractor = AcademicExtractor(api_key="...")
    result = extractor.extract("marksheet.jpg")

Exception handling::

    from academic_ocr.exceptions import (
        ImageQualityError,
        ExtractionError,
        FileValidationError,
    )

    try:
        result = extractor.extract("scan.jpg")
    except ImageQualityError:
        print("Image too blurry — ask user to re-upload")
    except FileValidationError:
        print("Invalid file type or file not found")
    except ExtractionError:
        print("Gemini API failure after retries")
"""

from .exceptions import (
    AcademicOCRError,
    ExtractionError,
    FileValidationError,
    ImageQualityError,
    ParseError,
)
from .extractor import AcademicExtractor

__all__ = [
    "AcademicExtractor",
    "AcademicOCRError",
    "FileValidationError",
    "ImageQualityError",
    "ExtractionError",
    "ParseError",
]
