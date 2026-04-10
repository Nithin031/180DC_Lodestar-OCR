"""
auth.py — API key authentication middleware for the academic_ocr API.

Provides a simple in-memory API key store with per-key owner metadata
and quota limits.  In production, replace the ``_KEY_STORE`` dict with
a database lookup.

Usage in FastAPI::

    from academic_ocr.auth import require_api_key

    @app.post("/extract")
    async def extract(key_meta: dict = Depends(require_api_key)):
        ...
"""

import logging
import os
from typing import Any, Dict

from fastapi import Header, HTTPException

# ── Module logger ─────────────────────────────────────────────────────
logger = logging.getLogger(__name__)

# ── Key store ─────────────────────────────────────────────────────────
# In production, replace this with a DB table or secrets manager lookup.
# Format: api_key_string -> { owner, quota_limit (requests per minute) }
_KEY_STORE: Dict[str, Dict[str, Any]] = {
    "test-key-001": {
        "owner": "development",
        "quota_limit": 60,
    },
    "test-key-002": {
        "owner": "integration-tests",
        "quota_limit": 30,
    },
}

# ── Load key from environment if set ──────────────────────────────────
_env_key = os.getenv("API_KEY")
if _env_key and _env_key not in _KEY_STORE:
    _KEY_STORE[_env_key] = {"owner": "env-configured", "quota_limit": 60}
    logger.info("Registered API key from API_KEY environment variable.")


def register_key(
    api_key: str,
    owner: str,
    quota_limit: int = 60,
) -> None:
    """Register a new API key at runtime.

    Args:
        api_key:     The key string.
        owner:       Human-readable owner name.
        quota_limit: Max requests per minute for this key.
    """
    _KEY_STORE[api_key] = {"owner": owner, "quota_limit": quota_limit}
    logger.info("API key registered for owner=%s (quota=%d/min)", owner, quota_limit)


def validate_key(api_key: str) -> Dict[str, Any]:
    """Validate an API key and return its metadata.

    Args:
        api_key: The key string from the ``X-API-Key`` header.

    Returns:
        A dict with ``owner`` and ``quota_limit`` fields.

    Raises:
        HTTPException: 401 Unauthorized if the key is not found.
    """
    meta = _KEY_STORE.get(api_key)
    if meta is None:
        logger.warning("Rejected invalid API key: %s…", api_key[:8])
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API key.",
        )
    return meta


def get_remaining_quota(api_key: str) -> int:
    """Return the configured quota limit for a key.

    This is the *configured* limit, not the *remaining* tokens — the
    rate limiter in ``ratelimit.py`` tracks actual consumption.

    Args:
        api_key: The key string.

    Returns:
        The quota limit (requests per minute), or 0 if the key is
        unknown.
    """
    meta = _KEY_STORE.get(api_key)
    return meta["quota_limit"] if meta else 0


async def require_api_key(
    x_api_key: str = Header(..., description="API key for authentication"),
) -> Dict[str, Any]:
    """FastAPI dependency that validates the ``X-API-Key`` header.

    Returns the key's metadata dict on success, or raises HTTP 401.

    Usage::

        @app.post("/extract")
        async def extract(key_meta: dict = Depends(require_api_key)):
            owner = key_meta["owner"]
            ...
    """
    return validate_key(x_api_key)
