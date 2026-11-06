"""Email verification by one-time code (OTP), sent through the Brevo HTTPS API.

(SMTP is blocked on Render's free tier, so we send over HTTPS instead.)

Flow
  1. POST /api/verify/send    {email}        -> emails a 6-digit code (10 min)
  2. POST /api/verify/confirm {email, code}  -> returns a one-time verification_token (30 min)
  3. The submit endpoints call claim_verification(email, token). It atomically
     consumes the token, so one verification allows exactly one submission.

Abuse controls
  - Cooldown:       one code per RESEND_COOLDOWN_SECONDS per email.
  - Send cap:       at most CODE_SENDS_PER_WINDOW codes per email per 24 hours.
                    Stored in the database, so it survives restarts and works
                    across every server instance.
  - Guess cap:      MAX_CODE_ATTEMPTS wrong guesses per code, counted atomically in Postgres.
  - IP limits:      generous, because offices often share one public IP.

Only HMAC hashes of the code and token are stored, never the raw values.

Environment variables
  BREVO_API_KEY         Brevo API key (starts with xkeysib-)
  MAIL_FROM_EMAIL       Sender address verified in Brevo
  VERIFICATION_SECRET   long random string used to hash codes/tokens
  MAIL_FROM_NAME        optional display name for the sender
  CODE_SENDS_PER_DAY    optional, default 10
"""
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request

from extensions import limiter
from http_utils import client_ip
from supabase_client import supabase

TABLE = "email_verification"

CODE_LENGTH = 6
CODE_TTL_SECONDS = 10 * 60
RESEND_COOLDOWN_SECONDS = 60
MAX_CODE_ATTEMPTS = 5
CODE_SENDS_PER_WINDOW = int(os.environ.get("CODE_SENDS_PER_DAY", "10"))
SEND_WINDOW_SECONDS = 24 * 60 * 60
TOKEN_TTL_SECONDS = 30 * 60

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
CODE_RE = re.compile(rf"^\d{{{CODE_LENGTH}}}$")

verification_bp = Blueprint("verification", __name__, url_prefix="/api/verify")


# ── Config ───────────────────────────────────────────────────
def _secret():
    return os.environ.get("VERIFICATION_SECRET", "")


def _mail_config():
    api_key = os.environ.get("BREVO_API_KEY", "").strip()
    sender = os.environ.get("MAIL_FROM_EMAIL", "").strip()
    return api_key, sender


def _is_configured():
    api_key, sender = _mail_config()
    return bool(_secret() and api_key and sender)


# ── Helpers ──────────────────────────────────────────────────
def _now():
    return datetime.now(timezone.utc)


def _parse_ts(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def normalize_email(value):
    return value.strip().lower() if isinstance(value, str) else ""


def _digest(purpose, email, value):
    msg = f"{purpose}:{email}:{value}".encode("utf-8")
    return hmac.new(_secret().encode("utf-8"), msg, hashlib.sha256).hexdigest()


def _scalar(data):
    """PostgREST may return a scalar or a one-item list. Normalize to the value."""
    if isinstance(data, list):
        return data[0] if data else None
    return data


def _get_record(email):
    rows = supabase.table(TABLE).select("*").eq("email", email).limit(1).execute().data
    return rows[0] if rows else None


def _update_record(email, fields):
    supabase.table(TABLE).update(fields).eq("email", email).execute()


def _limited(message, retry_after, code):
    """429 with a JSON body and a Retry-After header, so the frontend can show a clear wait time."""
    resp = jsonify({"error": message, "code": code, "retry_after": retry_after})
    resp.status_code = 429
    resp.headers["Retry-After"] = str(retry_after)
    return resp


def _send_window(rec, now):
    """Return (window_start, sends_so_far). A new window starts when the old one has expired."""
    start = _parse_ts(rec.get("send_window_started_at")) if rec else None
    if not start or now - start >= timedelta(seconds=SEND_WINDOW_SECONDS):
        return now, 0
    return start, int(rec.get("send_count") or 0)


def _send_code_email(to_email, code):
    api_key, sender = _mail_config()
    minutes = CODE_TTL_SECONDS // 60

    text = (
        f"Your verification code is: {code}\n\n"
        f"It expires in {minutes} minutes. Enter it on the request form to confirm your email address.\n\n"
        "If you did not request this code, you can safely ignore this email."
    )
    html = f"""\
<div style="font-family:Arial,sans-serif;max-width:420px;margin:0 auto;padding:24px;color:#0f1f3d;">
  <h2 style="margin:0 0 12px;font-size:18px;">Local Civil Registrar</h2>
  <p style="margin:0 0 16px;font-size:14px;">Use this code to confirm your email address:</p>
  <p style="margin:0 0 16px;font-size:32px;font-weight:700;letter-spacing:8px;">{code}</p>
  <p style="margin:0 0 8px;font-size:13px;color:#5577a0;">It expires in {minutes} minutes.</p>
  <p style="margin:0;font-size:13px;color:#5577a0;">If you did not request this code, you can safely ignore this email.</p>
</div>"""

    payload = {
        "sender": {
            "name": os.environ.get("MAIL_FROM_NAME", "Local Civil Registrar - San Carlos City"),
            "email": sender,
        },
        "to": [{"email": to_email}],
        "subject": "Your verification code - Local Civil Registrar",
        "htmlContent": html,
        "textContent": text,
    }
    req = urllib.request.Request(
        "https://api.brevo.com/v3/smtp/email",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "lcr-backend/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Brevo HTTP {e.code}: {e.read().decode('utf-8', 'replace')}") from e


# ── Public API used by request.py ────────────────────────────
def claim_verification(email, token):
    """Atomically consume a verified token. Returns True for exactly one caller.

    The conditional UPDATE is a single statement in Postgres, so when two
    submits race with the same token, only one matches the row.
    Tradeoff: if the insert fails after the claim, the token is spent and
    the user must verify again.
    """
    email = normalize_email(email)
    if not email or not isinstance(token, str) or not token or not _secret():
        return False
    try:
        res = (supabase.table(TABLE)
               .update({"token_hash": None, "token_expires_at": None})
               .eq("email", email)
               .eq("token_hash", _digest("token", email, token))
               .gt("token_expires_at", _now().isoformat())
               .execute())
    except Exception as e:
        print(f"[verify] claim failed: {e}")
        return False
    return bool(res.data)


# ── Routes ───────────────────────────────────────────────────
@verification_bp.post("/send")
@limiter.limit("100 per hour", key_func=client_ip)
def send_code():
    body = request.get_json(silent=True) or {}
    email = normalize_email(body.get("email"))
    if not email or not EMAIL_RE.match(email):
        return jsonify({"error": "Enter a valid email address."}), 400

    if not _is_configured():
        print("[verify] BREVO_API_KEY / MAIL_FROM_EMAIL / VERIFICATION_SECRET is not set")
        return jsonify({"error": "Email verification is temporarily unavailable."}), 503

    try:
        prev = _get_record(email) or {}
    except Exception as e:
        print(f"[verify] lookup failed: {e}")
        return jsonify({"error": "Something went wrong. Please try again."}), 500

    now = _now()

    # 1. Cooldown between two codes for the same email.
    last_sent = _parse_ts(prev.get("last_sent_at"))
    if last_sent:
        wait = RESEND_COOLDOWN_SECONDS - (now - last_sent).total_seconds()
        if wait > 0:
            seconds = math.ceil(wait)
            return _limited(f"Please wait {seconds} seconds before requesting another code.",
                            seconds, "COOLDOWN")

    # 2. Daily cap per email.
    window_start, sent_so_far = _send_window(prev, now)
    if sent_so_far >= CODE_SENDS_PER_WINDOW:
        reset_at = window_start + timedelta(seconds=SEND_WINDOW_SECONDS)
        seconds = max(1, math.ceil((reset_at - now).total_seconds()))
        return _limited("This email has reached today's limit for verification codes.",
                        seconds, "SEND_LIMIT")

    code = f"{secrets.randbelow(10 ** CODE_LENGTH):0{CODE_LENGTH}d}"
    try:
        supabase.table(TABLE).upsert({
            "email": email,
            "code_hash": _digest("code", email, code),
            "expires_at": (now + timedelta(seconds=CODE_TTL_SECONDS)).isoformat(),
            "attempts": 0,
            "last_sent_at": now.isoformat(),
            "send_count": sent_so_far + 1,
            "send_window_started_at": window_start.isoformat(),
            "token_hash": None,
            "token_expires_at": None,
            "verified_at": None,
        }, on_conflict="email").execute()
    except Exception as e:
        print(f"[verify] store failed: {e}")
        return jsonify({"error": "Something went wrong. Please try again."}), 500

    try:
        _send_code_email(email, code)
    except Exception as e:
        print(f"[verify] email send failed: {e}")
        # The email never left. Undo this attempt so it does not use up the cooldown or the daily cap.
        try:
            _update_record(email, {
                "code_hash": None,
                "expires_at": None,
                "last_sent_at": prev.get("last_sent_at"),
                "send_count": sent_so_far,
                "send_window_started_at": window_start.isoformat(),
            })
        except Exception as e2:
            print(f"[verify] rollback failed: {e2}")
        return jsonify({"error": "We couldn't send the verification email. Please try again in a moment."}), 502

    return jsonify({
        "ok": True,
        "expires_in": CODE_TTL_SECONDS,
        "resend_in": RESEND_COOLDOWN_SECONDS,
        "sends_left": CODE_SENDS_PER_WINDOW - (sent_so_far + 1),
    })


@verification_bp.post("/confirm")
@limiter.limit("100 per 15 minutes", key_func=client_ip)
def confirm_code():
    body = request.get_json(silent=True) or {}
    email = normalize_email(body.get("email"))
    code = body.get("code")
    code = code.strip() if isinstance(code, str) else ""

    if not email or not EMAIL_RE.match(email):
        return jsonify({"error": "Enter a valid email address."}), 400
    if not CODE_RE.match(code):
        return jsonify({"error": f"Enter the {CODE_LENGTH}-digit code."}), 400
    if not _is_configured():
        return jsonify({"error": "Email verification is temporarily unavailable."}), 503

    try:
        rec = _get_record(email)
    except Exception as e:
        print(f"[verify] lookup failed: {e}")
        return jsonify({"error": "Something went wrong. Please try again."}), 500

    if not rec or not rec.get("code_hash"):
        return jsonify({"error": "No active code for this email. Please request a new one."}), 400

    expires = _parse_ts(rec.get("expires_at"))
    if not expires or expires <= _now():
        return jsonify({"error": "This code has expired. Please request a new one."}), 400

    if int(rec.get("attempts") or 0) >= MAX_CODE_ATTEMPTS:
        return jsonify({"error": "Too many incorrect attempts. Please request a new code."}), 429

    # Atomic increment in Postgres, so parallel guesses cannot bypass the cap.
    try:
        attempts = _scalar(
            supabase.rpc("bump_verification_attempts", {"p_email": email}).execute().data
        )
    except Exception as e:
        print(f"[verify] attempt update failed: {e}")
        return jsonify({"error": "Something went wrong. Please try again."}), 500

    if attempts is None:
        return jsonify({"error": "No active code for this email. Please request a new one."}), 400
    attempts = int(attempts)
    if attempts > MAX_CODE_ATTEMPTS:
        return jsonify({"error": "Too many incorrect attempts. Please request a new code."}), 429

    if not hmac.compare_digest(rec["code_hash"].encode("utf-8"), _digest("code", email, code).encode("utf-8")):
        left = MAX_CODE_ATTEMPTS - attempts
        if left <= 0:
            return jsonify({"error": "Too many incorrect attempts. Please request a new code."}), 429
        return jsonify({"error": f"Incorrect code. {left} attempt{'s' if left != 1 else ''} left."}), 400

    token = secrets.token_urlsafe(32)
    now = _now()
    try:
        _update_record(email, {
            "code_hash": None,
            "expires_at": None,
            "attempts": 0,
            "token_hash": _digest("token", email, token),
            "token_expires_at": (now + timedelta(seconds=TOKEN_TTL_SECONDS)).isoformat(),
            "verified_at": now.isoformat(),
        })
    except Exception as e:
        print(f"[verify] token store failed: {e}")
        return jsonify({"error": "Something went wrong. Please try again."}), 500

    return jsonify({"ok": True, "verification_token": token, "expires_in": TOKEN_TTL_SECONDS})