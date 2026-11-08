import datetime
import hmac
from zoneinfo import ZoneInfo

from flask import Blueprint, jsonify, request

from config import (
    ALWAYS_REQUIRED,
    BUCKET,
    COMMON,
    KINDS,
    REQUIRED_TABLES,
    STATUS_LABELS,
    STATUS_ORDER,
)
from email_verification import claim_verification
from extensions import limiter
from notifications import create_request_notification, mask_name, subject_of
from signatures import (
    SignatureError,
    decode_signature_b64,
    extract_signature_from_upload,
    store_signature,
)
from supabase_client import supabase
from validators import clean, get_incoming_fields, validate_dates, validate_lengths, validate_row

requests_bp = Blueprint("requests", __name__)

TRACK_SELECT = (
    "id, control_no, status, created_at, updated_at, num_copies, requester_email, "
    "child_firstname, child_surname, deceased_firstname, deceased_surname, "
    "husband_fullname, wife_maiden_name"
)

PREFIX_TO_KIND = {cfg["prefix"].upper(): kind for kind, cfg in KINDS.items()}


def find_request(control_no):
    """Return (row, kind) for a control number, searching the right table first."""
    prefix = control_no.split("-", 1)[0]
    kinds = [PREFIX_TO_KIND[prefix]] if prefix in PREFIX_TO_KIND else list(KINDS)

    for kind in kinds:
        try:
            rows = (
                supabase.table(KINDS[kind]["table"])
                .select(TRACK_SELECT)
                .eq("control_no", control_no)
                .limit(1)
                .execute()
                .data
            )
        except Exception as e:
            print(f"[request] track lookup failed on {kind}: {e}")
            continue
        if rows:
            return rows[0], kind
    return None, None


def gen_control(prefix, record_id):
    today = datetime.datetime.now(ZoneInfo("Asia/Manila"))
    return f"{prefix}-{today:%Y%m%d}-{record_id:05d}"


def submit(kind):
    cfg = KINDS[kind]
    data, uploaded_file = get_incoming_fields(kind)
    if data is None:
        return jsonify({"error": "Request body must be a JSON object"}), 400

    row = {f: clean(data.get(f)) for f in cfg["fields"] + COMMON}
    row["status"] = "PENDING"
    row["record_type"] = kind

    # Explicit consent must be sent by the form (enforced server-side, not just in the UI).
    if str(data.get("consent", "")).strip().lower() not in ("true", "1", "yes"):
        return jsonify({"error": "Consent to the Privacy Policy is required to submit a request."}), 400

    missing = [f for f in cfg["required"] + ALWAYS_REQUIRED if not row.get(f)]
    if missing:
        return jsonify({"error": f"Missing required field(s): {', '.join(missing)}"}), 400

    field_errors = {**validate_lengths(row), **validate_row(row), **validate_dates(kind, row)}
    if field_errors:
        return jsonify({"error": " ".join(field_errors.values()), "fields": field_errors}), 400

    # Store emails lowercase so tracking and verification always compare the same value.
    row["requester_email"] = row["requester_email"].lower()

    # Atomically consume the verification token. Only one submit can win it.
    if not claim_verification(row["requester_email"], data.get("verification_token")):
        return jsonify({
            "error": "Please verify your email address before submitting your request.",
            "code": "EMAIL_NOT_VERIFIED",
        }), 403

    # Invalid signatures are rejected loudly instead of silently dropped.
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
        # single insert (DB trigger assigns control_no)
        rec = supabase.table(cfg["table"]).insert(row).execute().data[0]
        record_id = rec["id"]
        control_no = rec.get("control_no")
        if not control_no:  # trigger not installed yet: old behaviour
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


@requests_bp.get("/healthz")
@limiter.exempt
def healthz():
    """Cheap liveness check for the load balancer. No database call."""
    return {"status": "ok"}, 200


@requests_bp.get("/api/health")
@limiter.exempt
def health():
    """Manual diagnostics. Checks each table; don't point the load balancer here."""
    missing_tables = []
    for t in REQUIRED_TABLES + ["email_verification"]:
        try:
            supabase.table(t).select("*").limit(1).execute()
        except Exception:
            missing_tables.append(t)
    status = "ok" if not missing_tables else "degraded"
    db_status = "connected" if not missing_tables else "partial"
    return jsonify({
        "status": status,
        "db": db_status,
        "missing_tables": missing_tables,
        "missing": missing_tables,
    }), (200 if status == "ok" else 503)


submit_limit = limiter.shared_limit("3 per minute; 10 per hour", scope="submit")


@requests_bp.post("/api/birth/submit")
@submit_limit
def birth_submit():
    return submit("birth")


@requests_bp.post("/api/death/submit")
@submit_limit
def death_submit():
    return submit("death")


@requests_bp.post("/api/marriage/submit")
@submit_limit
def marriage_submit():
    return submit("marriage")


@requests_bp.post("/api/track")
@limiter.limit("20 per minute; 60 per 15 minutes")
def track_request():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400
    control_no = (data.get("control_no") or "").strip().upper()
    email = (data.get("email") or "").strip().lower()
    if not control_no or not email:
        return jsonify({"error": "Control number and email are required."}), 400

    not_found = (jsonify({"error": "No request matches that control number and email."}), 404)

    row, kind = find_request(control_no)
    stored = ((row or {}).get("requester_email") or "").strip().lower()
    # Same response whether the number or the email is wrong (no enumeration).
    # Bytes compare: str compare_digest raises on non-ASCII input.
    if not row or not stored or not hmac.compare_digest(stored.encode("utf-8"), email.encode("utf-8")):
        return not_found

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
