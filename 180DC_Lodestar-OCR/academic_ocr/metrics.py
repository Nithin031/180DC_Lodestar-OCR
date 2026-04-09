"""
metrics.py — Lightweight in-memory extraction metrics for the academic_ocr API.

Records one metric entry per extraction and exposes aggregation helpers
consumed by the ``GET /metrics`` endpoint in ``api.py``.

Storage is a bounded ``collections.deque(maxlen=10_000)`` — no external
dependencies required.  When you're ready for a real dashboard, replace
the ``record()`` calls with writes to InfluxDB or Prometheus; the
interface doesn't change.

Usage::

    from academic_ocr.metrics import record_metric, get_aggregates

    record_metric(
        job_id="abc-123",
        kind="marksheet",
        processing_ms=3820,
        cached=False,
        needs_review=True,
        subject_count=12,
        error_code=None,
    )

    stats = get_aggregates()
"""

import logging
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

# ── Module logger ─────────────────────────────────────────────────────
logger = logging.getLogger(__name__)

# ── Metric storage ───────────────────────────────────────────────────
_MAX_ENTRIES: int = 10_000
_metrics: Deque[Dict[str, Any]] = deque(maxlen=_MAX_ENTRIES)


def record_metric(
    job_id: str,
    kind: Optional[str],
    processing_ms: int,
    cached: bool,
    needs_review: bool,
    subject_count: int = 0,
    error_code: Optional[str] = None,
) -> None:
    """Append one extraction metric record.

    Args:
        job_id:        Unique identifier for the extraction job.
        kind:          Document kind — "marksheet", "certificate", or None
                       if extraction failed before classification.
        processing_ms: Wall-clock extraction time in milliseconds.
        cached:        ``True`` if the result was served from cache.
        needs_review:  ``True`` if the result was flagged for review.
        subject_count: Number of subjects extracted (marksheets only).
        error_code:    Exception class name if the extraction failed,
                       otherwise ``None``.
    """
    entry: Dict[str, Any] = {
        "timestamp": time.time(),
        "job_id": job_id,
        "kind": kind,
        "processing_ms": processing_ms,
        "cached": cached,
        "needs_review": needs_review,
        "subject_count": subject_count,
        "error_code": error_code,
    }
    _metrics.append(entry)
    logger.debug("Metric recorded: job_id=%s kind=%s", job_id, kind)


def get_aggregates() -> Dict[str, Any]:
    """Compute aggregate statistics over all recorded metrics.

    Returns:
        A dict containing:

        * ``total_extractions`` — total number of records
        * ``avg_latency_ms`` — average processing time (non-cached only)
        * ``avg_latency_by_kind`` — ``{kind: avg_ms}``
        * ``cache_hit_rate`` — fraction of cached results (0.0–1.0)
        * ``failure_rate`` — fraction of failed extractions
        * ``review_rate`` — fraction flagged for review
        * ``avg_subject_count`` — average subjects per marksheet
        * ``recent_errors`` — last 10 error codes
    """
    total = len(_metrics)

    if total == 0:
        return {
            "total_extractions": 0,
            "avg_latency_ms": 0,
            "avg_latency_by_kind": {},
            "cache_hit_rate": 0.0,
            "failure_rate": 0.0,
            "review_rate": 0.0,
            "avg_subject_count": 0.0,
            "recent_errors": [],
        }

    # ── Compute aggregates ────────────────────────────────────────────
    cached_count = 0
    failed_count = 0
    review_count = 0

    # For latency averages (exclude cached since processing_ms=0).
    latency_by_kind: Dict[str, List[int]] = {}
    non_cached_latencies: List[int] = []

    # For subject count average (marksheets only).
    subject_counts: List[int] = []

    # Recent errors.
    recent_errors: List[str] = []

    for entry in _metrics:
        if entry["cached"]:
            cached_count += 1
        else:
            non_cached_latencies.append(entry["processing_ms"])
            kind = entry.get("kind")
            if kind:
                latency_by_kind.setdefault(kind, []).append(entry["processing_ms"])

        if entry["error_code"]:
            failed_count += 1
            recent_errors.append(entry["error_code"])

        if entry["needs_review"]:
            review_count += 1

        if entry.get("kind") == "marksheet" and entry["subject_count"] > 0:
            subject_counts.append(entry["subject_count"])

    # ── Build result ──────────────────────────────────────────────────
    avg_latency = (
        round(sum(non_cached_latencies) / len(non_cached_latencies))
        if non_cached_latencies else 0
    )

    avg_by_kind = {
        kind: round(sum(vals) / len(vals))
        for kind, vals in latency_by_kind.items()
    }

    avg_subjects = (
        round(sum(subject_counts) / len(subject_counts), 1)
        if subject_counts else 0.0
    )

    return {
        "total_extractions": total,
        "avg_latency_ms": avg_latency,
        "avg_latency_by_kind": avg_by_kind,
        "cache_hit_rate": round(cached_count / total, 4),
        "failure_rate": round(failed_count / total, 4),
        "review_rate": round(review_count / total, 4),
        "avg_subject_count": avg_subjects,
        "recent_errors": recent_errors[-10:],
    }


def clear_metrics() -> int:
    """Clear all recorded metrics.

    Returns:
        Number of entries cleared.
    """
    count = len(_metrics)
    _metrics.clear()
    logger.info("Metrics cleared (%d entries).", count)
    return count
