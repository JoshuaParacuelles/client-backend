"""Small HTTP helpers. Rate limits use a shared store so every worker and instance agrees."""
from flask import request


def client_ip():
    # ProxyFix (set in request.py) makes remote_addr the real client.
    # Never parse X-Forwarded-For by hand.
    return request.remote_addr or "unknown"