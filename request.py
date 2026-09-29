import base64
import datetime
import hmac
import os
import re
import threading
import time
import uuid
from collections import defaultdict, deque
from functools import wraps

from dotenv import load_dotenv
load_dotenv()  # must run BEFORE supabase_client is imported

from flask import Flask, request, jsonify, Response
from flask_cors import CORS
from supabase_client import supabase

app = Flask(__name__)

# ── Config (all environment-driven) ──────────────────────────
# ALLOWED_ORIGINS: comma-separated list, e.g.
#   https://lcr-request.vercel.app,https://lcr-admin.vercel.app
# Defaults to "*" so local development keeps working, but SET IT in production.
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
# ADMIN_API_KEY: required for signature + notification endpoints. If unset,
# those endpoints are disabled (fail closed) instead of being public.
ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY")
DEBUG = os.environ.get("FLASK_DEBUG") == "1"

CORS(app, resources={r"/api/*": {"origins": ALLOWED_ORIGINS}})

BUCKET = "signatures"
MAX_SIG_BYTES = 2 * 1024 * 1024
app.config["MAX_CONTENT_LENGTH"] = MAX_SIG_BYTES + 512 * 1024  # file + form fields
EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "application/pdf": "pdf"}
MIME = {v: k for k, v in EXT.items()}

# ── Unified table ────────────────────────────────────────────
REQUEST_TABLE = "civil_registry_request"
REQUIRED_TABLES = [REQUEST_TABLE, "notification"]

KINDS = {
    "birth": {
        "table": REQUEST_TABLE, "prefix": "BR", "key": "birth_request",
        "fields": ["child_firstname", "child_middlename", "child_surname",
                   "birth_month", "birth_date", "birth_year", "place_of_birth"],
        "required": ["child_firstname", "birth_year"],
    },
    "death": {
        "table": REQUEST_TABLE, "prefix": "DR", "key": "death_request",
        "fields": ["deceased_firstname", "deceased_middlename", "deceased_surname",
                   "death_month", "death_date", "death_year", "place_of_death"],
        "required": ["deceased_firstname", "death_year"],
    },
    "marriage": {
        "table": REQUEST_TABLE, "prefix": "MR", "key": "marriage_request",
        "fields": ["husband_fullname", "wife_maiden_name", "marriage_date", "place_of_marriage"],
        "required": ["husband_fullname", "wife_maiden_name"],
    },
}
COMMON = ["num_copies", "purposes", "form_type",
          "requester_name", "requester_relationship", "requester_address", "requester_telephone",
          "requester_email",
          "registry_no", "date_of_registration", "book", "page", "search_by",
          "signature_printed_name"]
# requester_email is now enforced server-side too (the form already requires it).
ALWAYS_REQUIRED = ["requester_name", "requester_relationship", "requester_address", "requester_email"]

STATUS_ORDER = ["PENDING", "PROCESSING", "COMPLETED"]
STATUS_LABELS = {
    "PENDING": "Pending Review",
    "PROCESSING": "Being Processed",
    "COMPLETED": "Completed",
    "REJECTED": "Rejected",
}

# NOTE (migration): assumes civil_registry_request has an
# `updated_at timestamptz` column (nullable is fine).

# ── Validation patterns (mirror the frontend) ────────────────
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
PH_LOCAL_RE = re.compile(r"^0\d{10}$")
PH_INTL_RE = re.compile(r"^\+63\d{10}$")


# ── Helpers ──────────────────────────────────────────────────
def clean(v):
    """Trim strings; empty string -> None (Postgres rejects '' for date columns)."""
    if isinstance(v, str):
        return v.strip() or None
    return v


def gen_control(prefix, record_id):
    return f"{prefix}-{datetime.date.today():%Y%m%d}-{record_id:05d}"


def client_ip():
    # NOTE: X-Forwarded-For is only trustworthy behind your own proxy/host
    # (Render, Railway, nginx...). Otherwise a client can spoof it.
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() or request.remote_addr or "unknown")


# Simple in-memory sliding-window limiter. Fine for a single process; if you
# run several gunicorn workers, switch to Flask-Limiter with a Redis backend.
_hits = defaultdict(deque)
_hits_lock = threading.Lock()


def rate_limited(key, limit, window_seconds):
    now = time.monotonic()
    with _hits_lock:
        q = _hits[key]
        while q and now - q[0] > window_seconds:
            q.popleft()
        if len(q) >= limit:
            return True
        q.append(now)
        return False


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not ADMIN_API_KEY:
            return jsonify({"error": "Admin endpoints are disabled."}), 503
        supplied = request.headers.get("X-Admin-Key", "")
        if not hmac.compare_digest(supplied, ADMIN_API_KEY):
            return jsonify({"error": "Unauthorized"}), 401
        return fn(*args, **kwargs)
    return wrapper


@app.errorhandler(413)
def too_large(_e):
    return jsonify({"error": "Upload is too large. The signature file must be 2 MB or smaller."}), 413


# ── Signature handling ───────────────────────────────────────
class SignatureError(ValueError):
    """Raised when an uploaded signature is present but not acceptable."""


def sniff_mime(raw):
    """Detect the real type from magic bytes; never trust the client's header."""
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    if raw[:5] == b"%PDF-":
        return "application/pdf"
    return None


def validate_signature_bytes(raw):
    if len(raw) > MAX_SIG_BYTES:
        raise SignatureError("Signature file must be 2 MB or smaller.")
    mime = sniff_mime(raw)
    if not mime:
        raise SignatureError("Signature must be a PNG, JPG or WEBP image, or a PDF.")
    return raw, mime


def decode_signature_b64(sig):
    """Legacy JSON path. Returns (bytes, mime) or (None, None) if none was sent.
    Raises SignatureError if something was sent but is unusable."""
    if not sig:
        return None, None
    try:
        if sig.startswith("data:"):
            sig = sig.split(",", 1)[1]
        raw = base64.b64decode(sig, validate=True)
    except Exception:
        raise SignatureError("Signature data is not valid base64.")
    return validate_signature_bytes(raw)


def extract_signature_from_upload(file_storage):
    """Multipart path. Returns (bytes, mime) or (None, None) if no file was chosen.
    Raises SignatureError if a file was sent but is unusable."""
    if not file_storage or not file_storage.filename:
        return None, None
    raw = file_storage.read(MAX_SIG_BYTES + 1)
    if not raw:
        raise SignatureError("The uploaded signature file is empty.")
    return validate_signature_bytes(raw)


def store_signature(kind, raw, mime):
    if not raw:
        return None
    path = f"{kind}/{uuid.uuid4().hex}.{EXT[mime]}"
    supabase.storage.from_(BUCKET).upload(path, raw, {"content-type": mime})
    return path


# ── Display helpers ──────────────────────────────────────────
def subject_of(kind, r):
    if kind == "marriage":
        return f"{r.get('husband_fullname') or ''} & {r.get('wife_maiden_name') or ''}".strip()
    p = "child" if kind == "birth" else "deceased"
    return f"{r.get(p + '_firstname') or ''} {r.get(p + '_surname') or ''}".strip()


def mask_name(text):
    """'Juan Dela Cruz' -> 'J*** D*** C***' (privacy-safe display)."""
    return " ".join((w[0] + "***") if len(w) > 1 else w for w in (text or "").split())


def create_request_notification(supabase, row):
    """Insert an admin-bell notification for a newly submitted request."""
    try:
        kind = (row.get("record_type") or "").lower()
        if kind not in KINDS:
            return None

        subject = subject_of(kind, row)
        control_no = row.get("control_no")
        requester = row.get("requester_name") or "a citizen"

        supabase.table("notification").insert({
            "record_type": kind,
            "record_id": row.get("id"),
            "control_no": control_no,
            "title": f"New {kind.title()} Certificate Request",
            "message": f"New {kind} certificate request (Control No: {control_no}) "
                       f"for {subject or 'N/A'} from {requester}.",
            "request_snapshot": {
                "request_id": row.get("id"),
                "control_no": control_no,
                "record_type": kind,
                "requester_name": row.get("requester_name"),
                "num_copies": row.get("num_copies"),
                "purposes": row.get("purposes"),
                "source": "online_request",
            },
            "is_read": False,
        }).execute()
    except Exception as e:
        print(f"[request] notification insert failed (ignored): {e}")
        return None


def get_incoming_fields(kind):
    """Reads submitted data from JSON (legacy) or multipart/form-data.
    Returns (data_dict, uploaded_file_or_None)."""
    if (request.content_type or "").startswith("multipart/form-data"):
        return request.form.to_dict(), request.files.get("signature")
    body = request.get_json(silent=True) or {}
    return (body.get(KINDS[kind]["key"], {}) or {}), None


def validate_row(row):
    """Server-side mirror of the frontend rules. Returns {field: message}."""
    errs = {}
    email = row.get("requester_email")
    if email and not EMAIL_RE.match(email):
        errs["requester_email"] = "Enter a valid email address."
    phone = row.get("requester_telephone")
    if phone and not (PH_LOCAL_RE.match(phone) or PH_INTL_RE.match(phone)):
        errs["requester_telephone"] = "Enter a valid mobile number (09XXXXXXXXX or +639XXXXXXXXX)."
    return errs


# ── Submit ───────────────────────────────────────────────────
def submit(kind):
    if rate_limited(f"submit:{client_ip()}", limit=10, window_seconds=3600):
        return jsonify({"error": "Too many requests. Please try again later."}), 429

    cfg = KINDS[kind]
    data, uploaded_file = get_incoming_fields(kind)

    row = {f: clean(data.get(f)) for f in cfg["fields"] + COMMON}
    row["status"] = "PENDING"
    row["record_type"] = kind

    missing = [f for f in cfg["required"] + ALWAYS_REQUIRED if not row.get(f)]
    if missing:
        return jsonify({"error": f"Missing required field(s): {', '.join(missing)}"}), 400

    field_errors = validate_row(row)
    if field_errors:
        return jsonify({"error": " ".join(field_errors.values()), "fields": field_errors}), 400

    # Invalid signatures are now rejected loudly instead of silently dropped.
    try:
        if uploaded_file is not None:
            raw, mime = extract_signature_from_upload(uploaded_file)
        else:
            raw, mime = decode_signature_b64(data.get("signature_base64"))
    except SignatureError as e:
        return jsonify({"error": str(e)}), 400

    sig_path = None
    record_id = None
    try:
        sig_path = store_signature(kind, raw, mime)
        row["signature_path"] = sig_path
        rec = supabase.table(cfg["table"]).insert(row).execute().data[0]
        record_id = rec["id"]
        control_no = gen_control(cfg["prefix"], record_id)
        supabase.table(cfg["table"]).update({"control_no": control_no}).eq("id", record_id).execute()
    except Exception as e:
        print(f"[request] submit failed, rolling back: {e}")
        # Roll back so a retry can't create duplicates / orphaned files.
        if record_id is not None:
            try:
                supabase.table(cfg["table"]).delete().eq("id", record_id).execute()
            except Exception as e2:
                print(f"[request] rollback (row delete) failed: {e2}")
        if sig_path:
            try:
                supabase.storage.from_(BUCKET).remove([sig_path])
            except Exception as e2:
                print(f"[request] rollback (file delete) failed: {e2}")
        return jsonify({"error": "We couldn't save your request. Please try again."}), 500

    create_request_notification(supabase, {**row, **rec, "control_no": control_no})

    return jsonify({"record_id": record_id, "control_no": control_no,
                    "has_signature": sig_path is not None})


# ── Health ───────────────────────────────────────────────────
@app.get("/api/health")
def health():
    missing = []
    for t in REQUIRED_TABLES:
        try:
            supabase.table(t).select("id").limit(1).execute()
        except Exception:
            missing.append(t)
    if len(missing) == len(REQUIRED_TABLES):
        return jsonify({"db": "error", "error": "Cannot read any table. Check SUPABASE_URL / SUPABASE_KEY."}), 500
    return jsonify({"db": "connected", "missing": missing})


@app.post("/api/birth/submit")
def birth_submit():
    return submit("birth")


@app.post("/api/death/submit")
def death_submit():
    return submit("death")


@app.post("/api/marriage/submit")
def marriage_submit():
    return submit("marriage")


# ── PUBLIC TRACKING ──────────────────────────────────────────
# Changed from GET /api/track/<control_no> to POST /api/track so the email
# never appears in URLs/logs. The citizen must supply BOTH the control number
# and the email used on the request, so guessing control numbers gets an
# attacker nothing. Subject names are masked in the response.
@app.post("/api/track")
def track_request():
    if rate_limited(f"track:{client_ip()}", limit=10, window_seconds=900):
        return jsonify({"error": "Too many attempts. Please wait a few minutes and try again."}), 429

    body = request.get_json(silent=True) or {}
    control_no = (body.get("control_no") or "").strip().upper()
    email = (body.get("email") or "").strip().lower()
    if not control_no or not email:
        return jsonify({"error": "Control number and email are required."}), 400

    not_found = (jsonify({"error": "No request matches that control number and email."}), 404)

    try:
        rows = supabase.table(REQUEST_TABLE).select(
            "id, record_type, control_no, status, created_at, updated_at, "
            "num_copies, requester_email, "
            "child_firstname, child_surname, "
            "deceased_firstname, deceased_surname, "
            "husband_fullname, wife_maiden_name"
        ).eq("control_no", control_no).limit(1).execute().data
    except Exception as e:
        print(f"[request] track failed: {e}")
        return jsonify({"error": "Something went wrong. Please try again."}), 500

    row = rows[0] if rows else None
    stored = ((row or {}).get("requester_email") or "").strip().lower()
    # Same response whether the number or the email is wrong (no enumeration).
    if not row or not stored or not hmac.compare_digest(stored, email):
        return not_found

    kind = (row.get("record_type") or "birth").lower()
    status = (row.get("status") or "PENDING").upper()
    return jsonify({
        "control_no": row.get("control_no"),
        "record_type": kind,
        "subject": mask_name(subject_of(kind, row)),
        "num_copies": row.get("num_copies"),
        "status": status,
        "status_label": STATUS_LABELS.get(status, status),
        "is_rejected": status == "REJECTED",
        "submitted_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "steps": STATUS_ORDER,
        "step_labels": [STATUS_LABELS[s] for s in STATUS_ORDER],
        "current_step_index": STATUS_ORDER.index(status) if status in STATUS_ORDER else -1,
    })


# ── ADMIN-ONLY (require header  X-Admin-Key: <ADMIN_API_KEY>) ─
@app.get("/api/<string:record_type>/<int:record_id>/signature")
@admin_required
def get_signature(record_type, record_id):
    cfg = KINDS.get(record_type)
    if not cfg:
        return jsonify({"error": "Invalid record type"}), 400
    try:
        rows = (supabase.table(cfg["table"]).select("signature_path")
                .eq("id", record_id).eq("record_type", record_type).limit(1).execute().data)
        path = rows[0]["signature_path"] if rows else None
        if not path:
            return jsonify({"error": "Signature not found"}), 404
        raw = supabase.storage.from_(BUCKET).download(path)
        return Response(raw, mimetype=MIME.get(path.rsplit(".", 1)[-1], "application/octet-stream"))
    except Exception as e:
        print(f"[request] signature fetch failed: {e}")
        return jsonify({"error": "Could not load signature."}), 500


@app.get("/api/notifications")
@admin_required
def get_notifications():
    try:
        res = (supabase.table("notification").select("*")
               .order("created_at", desc=True).limit(100).execute())
        return jsonify(res.data)
    except Exception as e:
        print(f"[request] notifications failed: {e}")
        return jsonify({"error": "Could not load notifications."}), 500


@app.get("/api/notifications/unread-count")
@admin_required
def unread_count():
    try:
        res = supabase.table("notification").select("id", count="exact").eq("is_read", False).execute()
        return jsonify({"count": res.count})
    except Exception as e:
        print(f"[request] unread-count failed: {e}")
        return jsonify({"error": "Could not load count."}), 500


def _read_patch(read_by):
    return {"is_read": True, "read_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "read_by": read_by}


@app.patch("/api/notifications/<int:notif_id>/read")
@admin_required
def mark_read(notif_id):
    read_by = (request.get_json(silent=True) or {}).get("read_by")
    try:
        supabase.table("notification").update(_read_patch(read_by)).eq("id", notif_id).execute()
        return jsonify({"ok": True})
    except Exception as e:
        print(f"[request] mark_read failed: {e}")
        return jsonify({"error": "Could not update notification."}), 500


@app.patch("/api/notifications/mark-all-read")
@admin_required
def mark_all_read():
    read_by = (request.get_json(silent=True) or {}).get("read_by")
    try:
        supabase.table("notification").update(_read_patch(read_by)).eq("is_read", False).execute()
        return jsonify({"ok": True})
    except Exception as e:
        print(f"[request] mark_all_read failed: {e}")
        return jsonify({"error": "Could not update notifications."}), 500


if __name__ == "__main__":
    # Debugger only when explicitly enabled:  FLASK_DEBUG=1 python backend/request.py
    app.run(host="0.0.0.0", port=5001, debug=DEBUG)