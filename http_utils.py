"""Small HTTP helpers. Rate limits use a shared store so every worker and instance agrees."""
import os

from flask import request
from limits import RateLimitItemPerSecond
from limits.storage import storage_from_string
from limits.strategies import FixedWindowRateLimiter


def client_ip():
    # ProxyFix (set in request.py) makes remote_addr the real client.
    # Never parse X-Forwarded-For by hand.
    return request.remote_addr or "unknown"


_storage = storage_from_string(os.environ.get("RATELIMIT_STORAGE_URI", "memory://"))
_strategy = FixedWindowRateLimiter(_storage)


def rate_limited(key, limit, window_seconds):
    """True when `key` has already used `limit` calls in the window (shared across instances)."""
    try:
        return not _strategy.hit(RateLimitItemPerSecond(limit, window_seconds), "lcr", key)
    except Exception as e:
        # Fail open so a Redis outage doesn't take down the form.
        print(f"[ratelimit] store error, failing open: {e}")
        return False