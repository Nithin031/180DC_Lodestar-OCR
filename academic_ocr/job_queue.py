"""
queue.py — Async job queue for non-blocking document extraction.

Converts the synchronous ``POST /extract`` flow into an async pattern:

1. Client uploads a file → receives ``{"job_id": "...", "status": "queued"}``
   immediately.
2. A background worker thread picks up the job, runs
   ``extractor.extract()``, and stores the result.
3. If a ``webhook_url`` was provided, the result is POSTed to it.
4. Client polls ``GET /result/{job_id}`` to retrieve the final result.

The queue and job store are in-memory (``queue.Queue`` and a plain dict).
For multi-process deployments, swap these for Redis + Celery.

Usage::

    from academic_ocr.queue import job_queue, job_store, start_worker

    start_worker(extractor)  # Call once at app startup.
"""

import json
import logging
import os
import threading
import time
import uuid
from queue import Queue
from typing import Any, Callable, Dict, Optional

from .exceptions import AcademicOCRError
from .metrics import record_metric

# ── Module logger ─────────────────────────────────────────────────────
logger = logging.getLogger(__name__)

# ── Job status constants ─────────────────────────────────────────────
STATUS_QUEUED = "queued"
STATUS_PROCESSING = "processing"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

# ── Queue and store ──────────────────────────────────────────────────
job_queue: Queue[Dict[str, Any]] = Queue()
job_store: Dict[str, Dict[str, Any]] = {}

# ── Worker state ─────────────────────────────────────────────────────
_worker_thread: Optional[threading.Thread] = None
_worker_running: bool = False


def enqueue_job(
    filepath: str,
    webhook_url: Optional[str] = None,
) -> str:
    """Add a new extraction job to the queue.

    Args:
        filepath:    Path to the saved upload file.
        webhook_url: Optional URL to POST the result to on completion.

    Returns:
        The generated ``job_id`` (UUID4 hex string).
    """
    job_id = uuid.uuid4().hex
    job_entry: Dict[str, Any] = {
        "job_id": job_id,
        "filepath": filepath,
        "webhook_url": webhook_url,
        "status": STATUS_QUEUED,
        "result": None,
        "error": None,
        "enqueued_at": time.time(),
    }

    job_store[job_id] = job_entry
    job_queue.put(job_entry)

    logger.info(
        "Job enqueued: job_id=%s filepath=%s webhook=%s",
        job_id, filepath, webhook_url or "(none)",
    )
    return job_id


def get_job_status(job_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve the current state of a job.

    Args:
        job_id: The job identifier returned by :func:`enqueue_job`.

    Returns:
        A dict with ``status``, ``result``, and ``error`` fields, or
        ``None`` if the ``job_id`` is not found.
    """
    entry = job_store.get(job_id)
    if entry is None:
        return None

    response: Dict[str, Any] = {
        "job_id": job_id,
        "status": entry["status"],
    }

    if entry["status"] == STATUS_DONE and entry["result"] is not None:
        response.update(entry["result"])
    elif entry["status"] == STATUS_FAILED and entry["error"] is not None:
        response["error"] = entry["error"]

    return response


def _deliver_webhook(webhook_url: str, payload: Dict[str, Any]) -> None:
    """POST the extraction result to the client's webhook URL.

    Failures are logged but never raised — webhook delivery is
    best-effort.

    Args:
        webhook_url: The URL to POST to.
        payload:     The full extraction result dict.
    """
    try:
        import urllib.request

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            logger.info(
                "Webhook delivered: %s (status=%d)",
                webhook_url, resp.status,
            )
    except Exception as exc:
        logger.warning("Webhook delivery failed (%s): %s", webhook_url, exc)


def _worker_loop(extractor: Any) -> None:
    """Background worker that processes jobs from the queue.

    Runs in a daemon thread.  Pulls one job at a time, calls
    ``extractor.extract()``, stores the result, and optionally
    delivers a webhook.

    Args:
        extractor: An ``AcademicExtractor`` instance.
    """
    global _worker_running
    logger.info("Queue worker started.")

    while _worker_running:
        try:
            # Block for up to 1 second, then loop to check _worker_running.
            try:
                job = job_queue.get(timeout=1.0)
            except Exception:
                continue

            job_id = job["job_id"]
            filepath = job["filepath"]
            webhook_url = job.get("webhook_url")

            # Mark as processing.
            job["status"] = STATUS_PROCESSING
            logger.info("Processing job: %s", job_id)

            start_time = time.monotonic()
            error_code: Optional[str] = None

            try:
                result = extractor.extract(filepath)
                job["status"] = STATUS_DONE
                job["result"] = result
                logger.info("Job completed: %s (kind=%s)", job_id, result.get("kind"))

                # Record metric.
                subject_count = 0
                academic_record = result.get("academicRecord")
                if academic_record and isinstance(academic_record, dict):
                    subject_count = len(academic_record.get("subjects", []))

                record_metric(
                    job_id=job_id,
                    kind=result.get("kind"),
                    processing_ms=result.get("processing_ms", 0),
                    cached=result.get("cached", False),
                    needs_review=result.get("needs_review", False),
                    subject_count=subject_count,
                )

                # Deliver webhook if provided.
                if webhook_url:
                    _deliver_webhook(webhook_url, {"job_id": job_id, **result})

            except AcademicOCRError as exc:
                error_code = type(exc).__name__
                job["status"] = STATUS_FAILED
                job["error"] = str(exc)
                logger.error("Job failed: %s — %s: %s", job_id, error_code, exc)

                processing_ms = int((time.monotonic() - start_time) * 1000)
                record_metric(
                    job_id=job_id,
                    kind=None,
                    processing_ms=processing_ms,
                    cached=False,
                    needs_review=True,
                    error_code=error_code,
                )

                if webhook_url:
                    _deliver_webhook(webhook_url, {
                        "job_id": job_id,
                        "status": STATUS_FAILED,
                        "error": str(exc),
                    })

            except Exception as exc:
                error_code = type(exc).__name__
                job["status"] = STATUS_FAILED
                job["error"] = f"Unexpected error: {exc}"
                logger.exception("Unexpected error in job %s", job_id)

                processing_ms = int((time.monotonic() - start_time) * 1000)
                record_metric(
                    job_id=job_id,
                    kind=None,
                    processing_ms=processing_ms,
                    cached=False,
                    needs_review=True,
                    error_code=error_code,
                )

            finally:
                # Clean up the temp file.
                try:
                    if os.path.isfile(filepath):
                        os.unlink(filepath)
                        logger.debug("Temp file cleaned up: %s", filepath)
                except OSError as exc:
                    logger.warning("Failed to clean up temp file: %s", exc)

                job_queue.task_done()

        except Exception as exc:
            logger.exception("Worker loop error: %s", exc)

    logger.info("Queue worker stopped.")


def start_worker(extractor: Any) -> threading.Thread:
    """Start the background worker thread.

    Safe to call multiple times — only one worker will run.

    Args:
        extractor: An ``AcademicExtractor`` instance.

    Returns:
        The worker ``Thread`` object.
    """
    global _worker_thread, _worker_running

    if _worker_thread is not None and _worker_thread.is_alive():
        logger.warning("Worker already running.")
        return _worker_thread

    _worker_running = True
    _worker_thread = threading.Thread(
        target=_worker_loop,
        args=(extractor,),
        daemon=True,
        name="academic-ocr-worker",
    )
    _worker_thread.start()
    return _worker_thread


def stop_worker() -> None:
    """Signal the background worker to stop.

    The worker will finish its current job and exit.
    """
    global _worker_running
    _worker_running = False
    logger.info("Worker stop signal sent.")
