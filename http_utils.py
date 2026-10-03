"""Small HTTP helpers shared by request.py, email_verification.py and extensions.py."""
import threading
import time
from collections import defaultdict, deque

from flask import request


def client_ip():
    # Relies on werkzeug's ProxyFix (configured in request.py) to set
    # remote_addr from the trusted proxy hop. Never parse X-Forwarded-For
    # by hand: the leftmost value is controlled by the client.
    return request.remote_addr or "unknown"


# Simple in-memory sliding-window limiter. Per-process only: with several
# gunicorn workers each one keeps its own counts. Use Redis for shared state.
_hits = defaultdict(deque)
_hits_lock = threading.Lock()
_last_sweep = 0.0
_SWEEP_EVERY = 300  # seconds
_MAX_WINDOW = 3600  # longest window used anywhere (seconds)


def _sweep(now):
    """Drop keys whose newest hit is older than the longest window."""
    global _last_sweep
    if now - _last_sweep < _SWEEP_EVERY:
        return
    _last_sweep = now
    for k in [k for k, q in _hits.items() if not q or now - q[-1] > _MAX_WINDOW]:
        del _hits[k]


def rate_limited(key, limit, window_seconds):
    """Returns True when `key` has already made `limit` calls inside the window."""
    now = time.monotonic()
    with _hits_lock:
        _sweep(now)
        q = _hits[key]
        while q and now - q[0] > window_seconds:
            q.popleft()
        if len(q) >= limit:
            return True
        q.append(now)
        return False