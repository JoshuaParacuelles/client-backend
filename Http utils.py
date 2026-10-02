"""Small HTTP helpers shared by request.py and email_verification.py."""
import threading
import time
from collections import defaultdict, deque

from flask import request


def client_ip():
    # NOTE: X-Forwarded-For is only trustworthy behind your own proxy/host
    # (Render, Railway, nginx...). Otherwise a client can spoof it.
    fwd = request.headers.get("X-Forwarded-For", "")
    return fwd.split(",")[0].strip() or request.remote_addr or "unknown"


# Simple in-memory sliding-window limiter. Fine for a single process; if you
# run several gunicorn workers, switch to Flask-Limiter with a Redis backend.
_hits = defaultdict(deque)
_hits_lock = threading.Lock()


def rate_limited(key, limit, window_seconds):
    """Returns True when `key` has already made `limit` calls inside the window."""
    now = time.monotonic()
    with _hits_lock:
        q = _hits[key]
        while q and now - q[0] > window_seconds:
            q.popleft()
        if len(q) >= limit:
            return True
        q.append(now)
        return False