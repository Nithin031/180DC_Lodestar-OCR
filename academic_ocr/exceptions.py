"""
exceptions.py — Custom exception hierarchy for the academic_ocr module.

All public exceptions inherit from :class:`AcademicOCRError` so that
consumers can catch the entire family with a single ``except`` clause,
or handle specific failure modes individually.

Example::

    from academic_ocr.exceptions import ImageQualityError, ExtractionError

    try:
        result = extractor.extract("scan.jpg")
    except ImageQualityError as e:
        # Image was too blurry — ask the user to re-upload
        ...
    except ExtractionError as e:
        # Gemini API failed after retries
        ...
"""

__all__ = [
    "AcademicOCRError",
    "FileValidationError",
    "ImageQualityError",
    "ExtractionError",
    "ParseError",
]


class AcademicOCRError(Exception):
    """Base exception for every error raised by the academic_ocr module.

    Catching this exception will catch all module-specific errors.
    """


class FileValidationError(AcademicOCRError):
    """Raised when the input file fails validation.

    Common causes:
        * File does not exist on disk.
        * File extension is not one of the supported types.
        * File exceeds the maximum allowed size.
    """


class ImageQualityError(AcademicOCRError):
    """Raised when an image is too blurry or low-quality for reliable OCR.

    Attributes:
        sharpness_score: The computed Laplacian variance of the image.
        threshold:       The minimum acceptable sharpness score.
    """

    def __init__(
        self,
        message: str,
        sharpness_score: float,
        threshold: float,
    ) -> None:
        super().__init__(message)
        self.sharpness_score = sharpness_score
        self.threshold = threshold


class ExtractionError(AcademicOCRError):
    """Raised when the Gemini API call fails.

    This covers network errors, quota exhaustion, upload failures, and
    any other transient or permanent API-level error that occurs *after*
    local validation has passed.
    """


class ParseError(AcademicOCRError):
    """Raised when the Gemini response cannot be parsed into valid JSON.

    Attributes:
        raw_response: The raw string returned by Gemini (for debugging).
    """

    def __init__(self, message: str, raw_response: str | None = None) -> None:
        super().__init__(message)
        self.raw_response = raw_response
