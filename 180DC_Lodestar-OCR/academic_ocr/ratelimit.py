"""
ratelimit.py — Token-bucket rate limiter for the academic_ocr API.

Each API key gets a bucket of N tokens (equal to its ``quota_limit``),
refilled every 60 seconds.  Every request consumes one token.  When the
bucket is empty, the request is rejected with HTTP 429 and a
``Retry-After`` header.

For single-process deployments this uses a plain dict.  For multi-process
deployments behind Gunicorn/uvicorn workers, swap the dict for Redis
using INCR / EXPIRE — the interface stays the same.

Usage in FastAPI::

    from academic_ocr.ratelimit import check_rate_limit

    @app.post("/extract")
    async def extract(
        key_meta: dict = Depends(require_api_key),
        _rl: None = Depends(check_rate_limit),
    ):
        ...
"""

import logging
import time
from typing import Any, Dict, Tuple

from fastapi import Header, HTTPException

from .auth import validate_key

# ── Module logger ─────────────────────────────────────────────────────
logger = logging.getLogger(__name__)

# ── Bucket configuration ─────────────────────────────────────────────
_REFILL_INTERVAL_SECONDS: float = 60.0

# Bucket state: api_key -> (tokens_remaining, last_refill_timestamp)
_buckets: Dict[str, Tuple[int, float]] = {}


def _get_or_create_bucket(
    api_key: str,
    quota_limit: int,
) -> Tuple[int, float]:
    """Get the current bucket state, creating or refilling as needed.

    Args:
        api_key:     The API key string.
        quota_limit: Maximum tokens for this key's bucket.

    Returns:
        A ``(tokens_remaining, last_refill_timestamp)`` tuple.
    """
    now = time.time()

    if api_key not in _buckets:
        # First request for this key — initialise a full bucket.
        _buckets[api_key] = (quota_limit, now)
        return quota_limit, now

    tokens, last_refill = _buckets[api_key]
    elapsed = now - last_refill

    if elapsed >= _REFILL_INTERVAL_SECONDS:
        # Refill the bucket.
        tokens = quota_limit
        last_refill = now
        _buckets[api_key] = (tokens, last_refill)
        logger.debug("Bucket refilled for key=%s… (%d tokens)", api_key[:8], tokens)

    return tokens, last_refill


def consume_token(api_key: str, quota_limit: int) -> int:
    """Attempt to consume one token from the key's bucket.

    Args:
        api_key:     The API key string.
        quota_limit: Maximum tokens for this key's bucket.

    Returns:
        Number of tokens remaining *after* consumption.

    Raises:
        HTTPException: 429 Too Many Requests if the bucket is empty,
            with a ``Retry-After`` header indicating seconds until
            the next refill.
    """
    tokens, last_refill = _get_or_create_bucket(api_key, quota_limit)

    if tokens <= 0:
        seconds_until_refill = max(
            1,
            int(_REFILL_INTERVAL_SECONDS - (time.time() - last_refill)),
        )
        logger.warning(
            "Rate limit exceeded for key=%s… (retry after %ds)",
            api_key[:8], seconds_until_refill,
        )
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded.  Please try again later.",
            headers={"Retry-After": str(seconds_until_refill)},
        )

    remaining = tokens - 1
    _buckets[api_key] = (remaining, last_refill)
    logger.debug(
        "Token consumed for key=%s… (%d remaining)", api_key[:8], remaining,
    )
    return remaining


async def check_rate_limit(
    x_api_key: str = Header(..., description="API key for rate limiting"),
) -> None:
    """FastAPI dependency that enforces the token-bucket rate limit.

    Must be used *after* ``require_api_key`` in the dependency chain
    so that the key has already been validated.

    Usage::

        @app.post("/extract")
        async def extract(
            key_meta: dict = Depends(require_api_key),
            _rl: None = Depends(check_rate_limit),
        ):
            ...
    """
    meta = validate_key(x_api_key)
    consume_token(x_api_key, meta["quota_limit"])
