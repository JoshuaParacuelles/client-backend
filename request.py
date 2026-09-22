import base64
import datetime
import uuid

from dotenv import load_dotenv
load_dotenv()  # must run BEFORE supabase_client is imported

from flask import Flask, request, jsonify, Response
from flask_cors import CORS
from supabase_client import supabase

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

BUCKET = "signatures"
MAX_SIG_BYTES = 2 * 1024 * 1024
EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "application/pdf": "pdf"}
MIME = {v: k for k, v in EXT.items()}

# ── Unified table ────────────────────────────────────────────
# Birth, death, and marriage requests now live in ONE table
# (civil_registry_request), distinguished by the "record_type" column.
REQUEST_TABLE = "civil_registry_request"
REQUIRED_TABLES = [REQUEST_TABLE, "notification"]

# Per-type differences; everything else is shared.
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
          "requester_email",  # NEW: was missing, so the client's Gmail was collected on the
                               # form but silently dropped before saving. Required so status
                               # updates have an address to email the citizen at.
          "registry_no", "date_of_registration", "book", "page", "search_by",
          "signature_printed_name"]
ALWAYS_REQUIRED = ["requester_name", "requester_relationship", "requester_address"]

# ── STATUS TRACKING (used by the public /api/track lookup below) ──────
# Kept in sync with routes/citizen_requests.py in the main (admin)
# backend, which is the ONLY place that ever writes a new status. This
# file only reads it back for the citizen-facing tracker.
STATUS_ORDER = ["PENDING", "PROCESSING", "READY_FOR_PICKUP", "COMPLETED"]
STATUS_LABELS = {
    "PENDING": "Pending Review",
    "PROCESSING": "Being Processed",
    "READY_FOR_PICKUP": "Ready for Pickup",
    "COMPLETED": "Completed",
    "REJECTED": "Rejected",
}

# NOTE (migration): assumes civil_registry_request has an
# `updated_at timestamptz` column (nullable is fine). If it doesn't
# exist yet, add it:
#   ALTER TABLE civil_registry_request ADD COLUMN updated_at timestamptz;


def clean(v):
    """Trim strings; empty string -> None (Postgres rejects '' for date columns)."""
    if isinstance(v, str):
        return v.strip() or None
    return v


def gen_control(prefix, record_id):
    return f"{prefix}-{datetime.date.today():%Y%m%d}-{record_id:05d}"


def decode_signature_b64(sig):
    """Accepts plain base64 or a data-URI. Returns (bytes, mime) or (None, None).
    Used only for the legacy JSON submission path."""
    if not sig:
        return None, None
    try:
        mime = "image/jpeg"
        if sig.startswith("data:"):
            header, sig = sig.split(",", 1)
            mime = header[5:].split(";")[0] or mime
        raw = base64.b64decode(sig)
        if mime not in EXT or len(raw) > MAX_SIG_BYTES:
            return None, None
        return raw, mime
    except Exception:
        return None, None


def extract_signature_from_upload(file_storage):
    """Accepts a werkzeug FileStorage from request.files (multipart path).
    Returns (bytes, mime) or (None, None) if missing/invalid/too large."""
    if not file_storage or not file_storage.filename:
        return None, None
    raw = file_storage.read()
    if not raw:
        return None, None
    mime = file_storage.mimetype or "application/octet-stream"
    if mime not in EXT or len(raw) > MAX_SIG_BYTES:
        return None, None
    return raw, mime


def store_signature(kind, raw, mime):
    if not raw:
        return None
    path = f"{kind}/{uuid.uuid4().hex}.{EXT[mime]}"
    supabase.storage.from_(BUCKET).upload(path, raw, {"content-type": mime})
    return path


def who(kind, r):
    if kind == "marriage":
        return f"{r.get('husband_fullname')} & {r.get('wife_maiden_name')}"
    p = "child" if kind == "birth" else "deceased"
    return f"{r.get(p + '_firstname') or ''} {r.get(p + '_surname') or ''}".strip()


def get_incoming_fields(kind):
    """
    Reads the submitted field data regardless of whether the request came in
    as JSON (no file attached, legacy/simple path) or multipart/form-data
    (used automatically by the frontend whenever a signature file is chosen).

    Returns (data_dict, uploaded_file_or_None).
    """
    ctype = (request.content_type or "")

    if ctype.startswith("multipart/form-data"):
        # Frontend sends flat fields via FormData in this case (see
        # buildFormData in the React app), not nested under birth_request/etc.
        data = request.form.to_dict()
        file = request.files.get("signature")
        return data, file

    # Plain JSON body: fields are nested under the kind's key, e.g.
    # { "birth_request": { ... } }
    body = request.get_json(silent=True) or {}
    data = body.get(KINDS[kind]["key"], {}) or {}
    return data, None


def submit(kind):
    cfg = KINDS[kind]
    data, uploaded_file = get_incoming_fields(kind)

    row = {f: clean(data.get(f)) for f in cfg["fields"] + COMMON}
    row["status"] = "PENDING"
    row["record_type"] = kind  # distinguishes rows in the shared table

    missing = [f for f in cfg["required"] + ALWAYS_REQUIRED if not row.get(f)]
    if missing:
        return jsonify({"error": f"Missing required field(s): {', '.join(missing)}"}), 400

    # Prefer an actual uploaded file (multipart path); fall back to a
    # base64-encoded field for backward compatibility with plain-JSON callers.
    if uploaded_file is not None:
        raw, mime = extract_signature_from_upload(uploaded_file)
    else:
        raw, mime = decode_signature_b64(data.get("signature_base64"))

    try:
        row["signature_path"] = store_signature(kind, raw, mime)
        rec = supabase.table(cfg["table"]).insert(row).execute().data[0]
        record_id = rec["id"]
        control_no = gen_control(cfg["prefix"], record_id)
        supabase.table(cfg["table"]).update({"control_no": control_no}).eq("id", record_id).execute()

        snapshot = {**row, "record_type": kind, "control_no": control_no,
                    "has_signature": row["signature_path"] is not None}
        snapshot.pop("signature_path")
        supabase.table("notification").insert({
            "record_type": kind,
            "record_id": record_id,
            "control_no": control_no,
            "title": f"New {kind.title()} Request",
            "message": f"{kind.title()} record request for {who(kind, row)} submitted. Control No: {control_no}",
            "request_snapshot": snapshot,
        }).execute()

        return jsonify({"record_id": record_id, "control_no": control_no,
                        "has_signature": row["signature_path"] is not None})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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


# ── Submit ───────────────────────────────────────────────────
@app.post("/api/birth/submit")
def birth_submit():
    return submit("birth")


@app.post("/api/death/submit")
def death_submit():
    return submit("death")


@app.post("/api/marriage/submit")
def marriage_submit():
    return submit("marriage")


# ── PUBLIC TRACKING (NEW) ─────────────────────────────────────
@app.get("/api/track/<string:control_no>")
def track_request(control_no):
    """
    Public lookup — no auth. A citizen who submitted a request has their
    control number (returned at submission time) and can check its status
    without logging in. Only status/progress info is returned — never
    address, telephone, or signature data.

    The status here is only ever CHANGED by the main admin backend's
    routes/citizen_requests.py (session-gated, staff-only); this endpoint
    only reads whatever value is currently on the row.
    """
    control_no = (control_no or "").strip().upper()
    if not control_no:
        return jsonify({"error": "Control number is required."}), 400

    try:
        rows = supabase.table(REQUEST_TABLE).select(
            "id, record_type, control_no, status, created_at, updated_at, "
            "requester_name, num_copies, "
            "child_firstname, child_surname, "
            "deceased_firstname, deceased_surname, "
            "husband_fullname, wife_maiden_name"
        ).eq("control_no", control_no).limit(1).execute().data

        if not rows:
            return jsonify({"error": "No request found with that control number."}), 404

        row = rows[0]
        kind = (row.get("record_type") or "birth").lower()
        status = (row.get("status") or "PENDING").upper()

        return jsonify({
            "control_no": row.get("control_no"),
            "record_type": kind,
            "subject": who(kind, row),
            "requester_name": row.get("requester_name"),
            "num_copies": row.get("num_copies"),
            "status": status,
            "status_label": STATUS_LABELS.get(status, status),
            "is_rejected": status == "REJECTED",
            "submitted_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "steps": STATUS_ORDER,
            "current_step_index": STATUS_ORDER.index(status) if status in STATUS_ORDER else -1,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Signature image ──────────────────────────────────────────
@app.get("/api/<string:record_type>/<int:record_id>/signature")
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
        return jsonify({"error": str(e)}), 500


# ── Notifications ────────────────────────────────────────────
@app.get("/api/notifications")
def get_notifications():
    try:
        res = (supabase.table("notification").select("*")
               .order("created_at", desc=True).limit(100).execute())
        return jsonify(res.data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/api/notifications/unread-count")
def unread_count():
    try:
        res = supabase.table("notification").select("id", count="exact").eq("is_read", False).execute()
        return jsonify({"count": res.count})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _read_patch(read_by):
    return {"is_read": True, "read_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "read_by": read_by}


@app.patch("/api/notifications/<int:notif_id>/read")
def mark_read(notif_id):
    read_by = (request.json or {}).get("read_by")
    try:
        supabase.table("notification").update(_read_patch(read_by)).eq("id", notif_id).execute()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.patch("/api/notifications/mark-all-read")
def mark_all_read():
    read_by = (request.json or {}).get("read_by")
    try:
        supabase.table("notification").update(_read_patch(read_by)).eq("is_read", False).execute()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=True)