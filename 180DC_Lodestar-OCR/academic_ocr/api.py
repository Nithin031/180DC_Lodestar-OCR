"""
api.py — FastAPI application for the academic_ocr extraction service.

Provides these endpoints:

* ``POST /extract``        — synchronous extraction (blocks until done)
* ``POST /extract/async``  — async extraction (returns job_id immediately)
* ``GET  /result/{job_id}`` — poll for async job results
* ``GET  /health``         — health check
* ``GET  /metrics``        — extraction metrics and aggregates

All endpoints (except ``/health``) require an ``X-API-Key`` header and
are subject to token-bucket rate limiting.

Run with::

    uvicorn academic_ocr.api:app --reload --host 0.0.0.0 --port 8000
"""

import logging
import os
import tempfile
import time
import uuid

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from .auth import require_api_key
from .exceptions import (
    AcademicOCRError,
    ExtractionError,
    FileValidationError,
    ImageQualityError,
    ParseError,
)
from .extractor import AcademicExtractor
from .metrics import get_aggregates, record_metric
from .job_queue import enqueue_job, get_job_status, start_worker
from .ratelimit import check_rate_limit

# ── Bootstrap ─────────────────────────────────────────────────────────
load_dotenv()

logger = logging.getLogger(__name__)

# ── Configure logging ─────────────────────────────────────────────────
_log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _log_level, logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# ── Create the extractor singleton ────────────────────────────────────
_api_key = os.getenv("GEMINI_API_KEY")
if not _api_key:
    logger.warning(
        "GEMINI_API_KEY not set — the /extract endpoints will fail.  "
        "Set it in .env or as an environment variable."
    )
    _api_key = ""  # Allow app to start; will fail at extraction time.

extractor = AcademicExtractor(api_key=_api_key) if _api_key else None  # type: ignore[arg-type]

# ── FastAPI app ───────────────────────────────────────────────────────
app = FastAPI(
    title="Academic OCR API",
    description=(
        "Extract structured data from academic marksheets and "
        "certificates using Google Gemini."
    ),
    version="2.0.0",
)


# ── Global exception handler ─────────────────────────────────────────

# Map exception classes → (HTTP status, retryable flag)
_ERROR_MAP = {
    FileValidationError: (400, False),
    ImageQualityError:   (422, False),
    ParseError:          (502, False),
    ExtractionError:     (502, True),   # Network/quota — may succeed on retry.
    AcademicOCRError:    (500, False),  # Catch-all for unknown subtypes.
}


@app.exception_handler(AcademicOCRError)
async def academic_ocr_error_handler(request, exc: AcademicOCRError):
    """Structured error response for all AcademicOCRError subtypes."""
    exc_type = type(exc)
    status, retryable = _ERROR_MAP.get(exc_type, (500, False))

    return JSONResponse(
        status_code=status,
        content={
            "error": str(exc),
            "code": exc_type.__name__,
            "retryable": retryable,
        },
    )


# ── Startup event — launch the async worker ──────────────────────────

@app.on_event("startup")
async def on_startup():
    """Start the background queue worker when the app boots."""
    if extractor is not None:
        start_worker(extractor)
        logger.info("Background worker started.")
    else:
        logger.error("Cannot start worker — extractor not initialised.")


# ══════════════════════════════════════════════════════════════════════
#  Endpoints
# ══════════════════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    """Health check — always returns ``{"status": "ok"}``.

    No authentication required.
    """
    return {"status": "ok"}


@app.post("/extract")
async def extract_sync(
    file: UploadFile = File(..., description="Document image or PDF"),
    key_meta: dict = Depends(require_api_key),
    _rl: None = Depends(check_rate_limit),
):
    """Synchronous extraction — blocks until Gemini responds.

    **Headers:**
        ``X-API-Key: <your-key>``

    **Body:**
        Multipart form data with a ``file`` field.

    **Returns:**
        The extraction result dict with ``kind``, ``needs_review``,
        ``processing_ms``, ``cached``, and type-specific fields.
    """
    if extractor is None:
        raise HTTPException(500, detail="Extractor not initialised — check GEMINI_API_KEY.")

    # ── Save upload to temp file ──────────────────────────────────────
    original_ext = ""
    if file.filename:
        original_ext = os.path.splitext(file.filename)[1].lower()

    tmp = tempfile.NamedTemporaryFile(
        delete=False, suffix=original_ext, dir=_get_temp_dir(),
    )
    try:
        content = await file.read()
        tmp.write(content)
        tmp.close()

        # ── Run extraction ────────────────────────────────────────────
        job_id = uuid.uuid4().hex
        result = extractor.extract(tmp.name)

        # ── Record metric ─────────────────────────────────────────────
        subject_count = 0
        ar = result.get("academicRecord")
        if ar and isinstance(ar, dict):
            subject_count = len(ar.get("subjects", []))

        record_metric(
            job_id=job_id,
            kind=result.get("kind"),
            processing_ms=result.get("processing_ms", 0),
            cached=result.get("cached", False),
            needs_review=result.get("needs_review", False),
            subject_count=subject_count,
        )

        return result

    finally:
        # ── Clean up temp file ────────────────────────────────────────
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


@app.post("/extract/async")
async def extract_async(
    file: UploadFile = File(..., description="Document image or PDF"),
    webhook_url: str = Form(default=None, description="URL to POST results to"),
    key_meta: dict = Depends(require_api_key),
    _rl: None = Depends(check_rate_limit),
):
    """Async extraction — returns a job_id immediately.

    The extraction runs in a background worker thread.  Poll
    ``GET /result/{job_id}`` to check status, or provide a
    ``webhook_url`` to receive the result via POST callback.

    **Headers:**
        ``X-API-Key: <your-key>``

    **Body:**
        Multipart form data with ``file`` and optional ``webhook_url``.

    **Returns:**
        ``{"job_id": "...", "status": "queued"}``
    """
    if extractor is None:
        raise HTTPException(500, detail="Extractor not initialised — check GEMINI_API_KEY.")

    # ── Save upload to temp file (worker cleans up) ───────────────────
    original_ext = ""
    if file.filename:
        original_ext = os.path.splitext(file.filename)[1].lower()

    tmp = tempfile.NamedTemporaryFile(
        delete=False, suffix=original_ext, dir=_get_temp_dir(),
    )
    content = await file.read()
    tmp.write(content)
    tmp.close()

    # ── Enqueue ───────────────────────────────────────────────────────
    job_id = enqueue_job(filepath=tmp.name, webhook_url=webhook_url)

    return {"job_id": job_id, "status": "queued"}


@app.get("/result/{job_id}")
async def get_result(
    job_id: str,
    key_meta: dict = Depends(require_api_key),
):
    """Poll for the result of an async extraction job.

    **Headers:**
        ``X-API-Key: <your-key>``

    **Returns:**
        * ``{"status": "queued"}`` — still waiting
        * ``{"status": "processing"}`` — currently running
        * ``{"status": "done", ...result}`` — completed
        * ``{"status": "failed", "error": "..."}`` — failed
    """
    status = get_job_status(job_id)
    if status is None:
        raise HTTPException(404, detail=f"Job not found: {job_id}")
    return status


@app.get("/metrics")
async def metrics(
    key_meta: dict = Depends(require_api_key),
):
    """Return aggregated extraction metrics.

    **Headers:**
        ``X-API-Key: <your-key>``

    **Returns:**
        Aggregated stats: avg latency, cache hit rate, failure rate, etc.
    """
    return get_aggregates()


# ── Helpers ───────────────────────────────────────────────────────────

def _get_temp_dir() -> str:
    """Return the temp directory for uploaded files.

    Creates ``academic_ocr/_tmp/`` next to this file if it doesn't exist.
    """
    tmp_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "_tmp",
    )
    os.makedirs(tmp_dir, exist_ok=True)
    return tmp_dir
