"""
schemas.py — Typed schema definitions for academic document extraction.

Defines TypedDict structures used both as Python type hints and as
Gemini structured-output response schemas.
"""

from typing import List, Optional
from typing_extensions import TypedDict


class SubjectEntry(TypedDict):
    """A single subject row from a marksheet.

    Attributes:
        subject:     Name of the subject (e.g. "Mathematics" or "PHYSICAL & HEALTH EDUCATION").
        score:       Score obtained (string), or None if it's a grade-only subject.
        maxScore:    Maximum possible score (string), or None if it's a grade-only subject.
        grade:       Letter grade if available, otherwise None.
        gradingType: "marks" if scores are present, or "grade_only" if the subject only uses grades (e.g., "XX" or blank in score columns).
        confidence:  OCR confidence for this row (0.0–1.0).
    """
    subject: str
    score: Optional[str]
    maxScore: Optional[str]
    grade: Optional[str]
    gradingType: str
    confidence: Optional[float]


class AcademicRecord(TypedDict):
    """Aggregate academic performance for a marksheet.

    Attributes:
        gradingMode: One of "percentage", "sgpa", "cgpa", or "grade".
        percentage:  Overall percentage if applicable, otherwise None.
        sgpa:        Semester GPA if applicable, otherwise None.
        cgpa:        Cumulative GPA if applicable, otherwise None.
        subjects:    List of individual subject entries.
    """
    gradingMode: str
    percentage: Optional[float]
    sgpa: Optional[float]
    cgpa: Optional[float]
    subjects: List[SubjectEntry]


class DocumentExtraction(TypedDict):
    """Unified polymorphic schema returned by Gemini for any academic document.

    The ``kind`` field determines which subset of fields is meaningful:

    * **marksheet** — ``title``, ``exam_type``, ``academicRecord``, ``tags``
    * **certificate** — ``title``, ``recipient``, ``achievement``, ``date``, ``tags``
    * **unknown** — all other fields will be ``None``

    Attributes:
        kind:           Document classification — "marksheet", "certificate", or "unknown".
        title:          Document title extracted from the image/PDF.
        exam_type:      Type of examination (marksheet only): "midterm", "final", "unit test", etc.
        academicRecord: Structured academic results (marksheet only).
        recipient:      Name of the person the certificate is awarded to (certificate only).
        achievement:    Description of the accomplishment (certificate only).
        date:           Date on the certificate in YYYY-MM-DD format (certificate only).
        tags:           2–4 auto-generated descriptive tags.
    """
    kind: str
    title: Optional[str]
    exam_type: Optional[str]
    academicRecord: Optional[AcademicRecord]
    recipient: Optional[str]
    achievement: Optional[str]
    date: Optional[str]
    tags: List[str]
