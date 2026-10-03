import os
from flask_limiter import Limiter
from http_utils import client_ip

limiter = Limiter(
    key_func=client_ip,
    default_limits=["300 per hour"],
    # Use Redis in production (e.g. Upstash): redis://... In-memory is per-process only.
    storage_uri=os.environ.get("RATELIMIT_STORAGE_URI", "memory://"),
    headers_enabled=True,
)