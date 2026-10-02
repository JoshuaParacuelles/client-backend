"""Email verification by one-time code (OTP), sent through Gmail SMTP.

Flow
  1. POST /api/verify/send    {email}        -> emails a 6-digit code (10 min)
  2. POST /api/verify/confirm {email, code}  -> returns a one-time verification_token (30 min)
  3. The submit endpoints call is_email_verified(email, token) before saving a
     request, then consume_verification(email) so the token cannot be reused.

Only HMAC hashes of the code and token are stored, never the raw values.

Environment variables
  GMAIL_USER           Gmail address that sends the codes
  GMAIL_APP_PASSWORD   16-character Google App Password (needs 2-Step Verification)
  VERIFICATION_SECRET  long random string used to hash codes/tokens
  MAIL_FROM_NAME       optional display name for the sender
"""
import hashlib
import hmac
import math
import os
import re
import secrets
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr

from flask import Blueprint, jsonify, request

from http_utils import client_ip, rate_limited
from supabase_client import supabase

TABLE = "email_verification"

CODE_LENGTH = 6
CODE_TTL_SECONDS = 10 * 60        # how long a code is valid
RESEND_COOLDOWN_SECONDS = 60      # minimum gap between two codes for one email
MAX_ATTEMPTS = 5                  # wrong guesses allowed per code
TOKEN_TTL_SECONDS = 30 * 60       # how long a verified email may be used to submit

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
CODE_RE = re.compile(rf"^\d{{{CODE_LENGTH}}}$")

verification_bp = Blueprint("verification", __name__, url_prefix="/api/verify")


# ── Config ───────────────────────────────────────────────────
def _secret():
    return os.environ.get("VERIFICATION_SECRET", "")


def _mail_credentials():
    user = os.environ.get("GMAIL_USER", "").strip()
    # Google displays app passwords in groups separated by spaces.
    password = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
    return user, password


def _is_configured():
    user, password = _mail_credentials()
    return bool(_secret() and user and password)


# ── Helpers ──────────────────────────────────────────────────
def _now():
    return datetime.now(timezone.utc)


def _parse_ts(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def normalize_email(value):
    return value.strip().lower() if isinstance(value, str) else ""


def _digest(purpose, email, value):
    msg = f"{purpose}:{email}:{value}".encode("utf-8")
    return hmac.new(_secret().encode("utf-8"), msg, hashlib.sha256).hexdigest()


def _get_record(email):
    rows = supabase.table(TABLE).select("*").eq("email", email).limit(1).execute().data
    return rows[0] if rows else None


def _update_record(email, fields):
    supabase.table(TABLE).update(fields).eq("email", email).execute()


def _send_code_email(to_email, code):
    user, password = _mail_credentials()
    minutes = CODE_TTL_SECONDS // 60

    msg = EmailMessage()
    msg["Subject"] = "Your verification code - Local Civil Registrar"
    msg["From"] = formataddr((os.environ.get("MAIL_FROM_NAME", "Local Civil Registrar - San Carlos City"), user))
    msg["To"] = to_email
    msg.set_content(
        f"Your verification code is: {code}\n\n"
        f"It expires in {minutes} minutes. Enter it on the request form to confirm your email address.\n\n"
        "If you did not request this code, you can safely ignore this email."
    )
    msg.add_alternative(
        f"""\
<div style="font-family:Arial,sans-serif;max-width:420px;margin:0 auto;padding:24px;color:#0f1f3d;">
  <h2 style="margin:0 0 12px;font-size:18px;">Local Civil Registrar</h2>
  <p style="margin:0 0 16px;font-size:14px;">Use this code to confirm your email address:</p>
  <p style="margin:0 0 16px;font-size:32px;font-weight:700;letter-spacing:8px;">{code}</p>
  <p style="margin:0 0 8px;font-size:13px;color:#5577a0;">It expires in {minutes} minutes.</p>
  <p style="margin:0;font-size:13px;color:#5577a0;">If you did not request this code, you can safely ignore this email.</p>
</div>""",
        subtype="html",
    )

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context, timeout=15) as server:
        server.login(user, password)
        server.send_message(msg)


# ── Public API used by request.py ────────────────────────────
def is_email_verified(email, token):
    """True only if `token` is the unexpired token issued for `email`."""
    email = normalize_email(email)
    if not email or not isinstance(token, str) or not token or not _secret():
        return False
    try:
        rec = _get_record(email)
    except Exception as e:
        print(f"[verify] lookup failed: {e}")
        return False
    if not rec or not rec.get("token_hash"):
        return False
    expires = _parse_ts(rec.get("token_expires_at"))
    if not expires or expires <= _now():
        return False
    return hmac.compare_digest(rec["token_hash"], _digest("token", email, token))


def consume_verification(email):
    """Invalidates the token so one verification allows exactly one submission."""
    email = normalize_email(email)
    if not email:
        return
    try:
        _update_record(email, {"token_hash": None, "token_expires_at": None})
    except Exception as e:
        print(f"[verify] consume failed (ignored): {e}")


# ── Routes ───────────────────────────────────────────────────
@verification_bp.post("/send")
def send_code():
    body = request.get_json(silent=True) or {}
    email = normalize_email(body.get("email"))
    if not email or not EMAIL_RE.match(email):
        return jsonify({"error": "Enter a valid email address."}), 400

    if not _is_configured():
        print("[verify] GMAIL_USER / GMAIL_APP_PASSWORD / VERIFICATION_SECRET is not set")
        return jsonify({"error": "Email verification is temporarily unavailable."}), 503

    if rate_limited(f"verify-send-ip:{client_ip()}", limit=10, window_seconds=3600) or \
            rate_limited(f"verify-send-email:{email}", limit=5, window_seconds=3600):
        return jsonify({"error": "Too many code requests. Please try again later."}), 429

    try:
        existing = _get_record(email)
    except Exception as e:
        print(f"[verify] lookup failed: {e}")
        return jsonify({"error": "Something went wrong. Please try again."}), 500

    if existing:
        last_sent = _parse_ts(existing.get("last_sent_at"))
        if last_sent:
            wait = RESEND_COOLDOWN_SECONDS - (_now() - last_sent).total_seconds()
            if wait > 0:
                retry_after = math.ceil(wait)
                return jsonify({
                    "error": f"Please wait {retry_after} seconds before requesting another code.",
                    "retry_after": retry_after,
                }), 429

    code = f"{secrets.randbelow(10 ** CODE_LENGTH):0{CODE_LENGTH}d}"
    now = _now()
    try:
        supabase.table(TABLE).upsert({
            "email": email,
            "code_hash": _digest("code", email, code),
            "expires_at": (now + timedelta(seconds=CODE_TTL_SECONDS)).isoformat(),
            "attempts": 0,
            "last_sent_at": now.isoformat(),
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
        # Drop the unsent code so the cooldown does not block an immediate retry.
        try:
            supabase.table(TABLE).delete().eq("email", email).execute()
        except Exception as e2:
            print(f"[verify] cleanup failed: {e2}")
        return jsonify({"error": "We couldn't send the verification email. Please try again in a moment."}), 502

    return jsonify({
        "ok": True,
        "expires_in": CODE_TTL_SECONDS,
        "resend_in": RESEND_COOLDOWN_SECONDS,
    })


@verification_bp.post("/confirm")
def confirm_code():
    if rate_limited(f"verify-confirm-ip:{client_ip()}", limit=30, window_seconds=900):
        return jsonify({"error": "Too many attempts. Please wait a few minutes and try again."}), 429

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

    attempts = int(rec.get("attempts") or 0)
    if attempts >= MAX_ATTEMPTS:
        return jsonify({"error": "Too many incorrect attempts. Please request a new code."}), 429

    # Count the attempt before comparing, so parallel guesses cannot skip the limit.
    attempts += 1
    try:
        _update_record(email, {"attempts": attempts})
    except Exception as e:
        print(f"[verify] attempt update failed: {e}")
        return jsonify({"error": "Something went wrong. Please try again."}), 500

    if not hmac.compare_digest(rec["code_hash"], _digest("code", email, code)):
        left = MAX_ATTEMPTS - attempts
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