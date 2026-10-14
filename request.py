"""
routes/citizen_requests.py
---------------------------
Session-gated management of citizen certificate requests
(civil_registry_request table). This blueprint belongs to the MAIN app
(the one with /api/session, /api/logout, is_admin()/get_user_permissions())
— NOT to backend/request.py, which is a separate, unauthenticated Flask
process serving the public request website.

NOTIFICATION BEHAVIOR:
  * Every status change (Being Processed, Completed, Rejected) creates an
    admin-bell notification with a full request snapshot, so the modal can
    show the details. Pending Review is the starting state and never notifies.
  * The citizen is emailed ONLY for Completed and Rejected
    (see NOTIFY_ON_STATUSES).

NOTE: bell notifications created here are pushed over SSE immediately,
because this blueprint runs in the same process as routes/notification.py.
Notifications created by backend/request.py (new submissions) are written to
the database only, so the bell shows them after a refresh or a poll.

SIGNATURE (detail endpoint):
  The signature file lives in the private Supabase Storage bucket
  "signatures". The detail endpoint returns `signature_url`, a short-lived
  signed URL. The raw storage path (`signature_path`) is never exposed.
"""

import traceback
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import datetime, timezone
from functools import wraps

from flask import Blueprint, request, jsonify, session, Response

from supabase_client import supabase
from auth.Rolemanagement import is_admin, get_user_permissions
from .email_service import send_status_update_email
from .notification import push_notification
from logs.Audits import record_action
from security import audit_denial

citizen_requests_bp = Blueprint("citizen_requests_bp", __name__)

REQUEST_TABLE = "civil_registry_request"

STATUS_ORDER = ["PENDING", "PROCESSING", "COMPLETED"]
STATUS_LABELS = {
    "PENDING": "Pending Review",
    "PROCESSING": "Being Processed",
    "COMPLETED": "Completed",
    "REJECTED": "Rejected",
}
ALL_STATUSES = list(STATUS_LABELS.keys())

# Valid values for the "notify_via" field the frontend sends. Anything
# else falls back to "email".
VALID_NOTIFY_VIA = {"email", "none"}

# The citizen is emailed ONLY when a request is completed or rejected.
# Change this set to add PROCESSING if you want emails for that status too.
NOTIFY_ON_STATUSES = {"COMPLETED", "REJECTED"}

# Admin bell: any real status change creates a notification.
BELL_ON_STATUSES = {"PROCESSING", "COMPLETED", "REJECTED"}

# Columns never copied into a notification snapshot.
SNAPSHOT_EXCLUDE = {"signature_path"}

# Permission key the frontend checks via hasAccess("citizen_requests").
REQUIRED_PERMISSION = "citizen_requests"

# Max time (seconds) to wait for the email before giving up on it.
NOTIFY_TIMEOUT_SECONDS = 15

# Private bucket written by backend/request.py.
SIGNATURE_BUCKET = "signatures"
SIGNATURE_URL_TTL_SECONDS = 3600
SIGNATURE_MIME = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "pdf": "application/pdf",
}


def get_user():
    return session.get("username", "System")


def _staff_required(fn):
    """Allows an admin unconditionally, or a non-admin whose permission list
    includes REQUIRED_PERMISSION."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        username = session.get("username")
        if not username:
            audit_denial("Citizen request route requires authentication")
            return jsonify({"error": "Not logged in"}), 401
        if is_admin(username):
            return fn(*args, **kwargs)
        perms = get_user_permissions(username) or []
        if REQUIRED_PERMISSION in perms or "*" in perms:
            return fn(*args, **kwargs)
        audit_denial("Citizen request permission required")
        return jsonify({"error": "Forbidden"}), 403
    return wrapper


def who(kind, r):
    if kind == "marriage":
        return f"{r.get('husband_fullname')} & {r.get('wife_maiden_name')}"
    p = "child" if kind == "birth" else "deceased"
    return f"{r.get(p + '_firstname') or ''} {r.get(p + '_surname') or ''}".strip()


def _signed_signature_url(path):
    """Short-lived signed URL for a file in the private signatures bucket.
    Returns None (never raises) if the file is missing or signing fails."""
    if not path:
        return None
    try:
        res = supabase.storage.from_(SIGNATURE_BUCKET).create_signed_url(
            path, SIGNATURE_URL_TTL_SECONDS
        )
        return (res or {}).get("signedURL") or (res or {}).get("signedUrl")
    except Exception as e:
        print(f"[citizen_requests] could not sign signature URL for {path}: {e}")
        return None


def _send_email_notification(to_email, subject, body):
    """Send the status-update email in a worker thread, with a hard timeout.
    Returns a plain boolean. NEVER raises."""
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(
        send_status_update_email,
        to_email=to_email,
        subject=subject,
        body=body,
    )

    sent = False
    try:
        sent = bool(future.result(timeout=NOTIFY_TIMEOUT_SECONDS))
    except FuturesTimeout:
        print(
            f"[citizen_requests] email notification did not finish within "
            f"{NOTIFY_TIMEOUT_SECONDS}s, giving up on it (status update is unaffected)."
        )
    except Exception as e:
        print(f"[citizen_requests] email notification raised: {e}")
        traceback.print_exc()

    executor.shutdown(wait=False)
    return sent


def _safe_record_action(*args, **kwargs):
    """Audit logging is nice-to-have; it must never fail the request."""
    try:
        record_action(*args, **kwargs)
    except Exception as e:
        print(f"[citizen_requests] record_action failed (ignored): {e}")
        traceback.print_exc()


def build_status_snapshot(kind, row, new_status, updated_at):
    """Copy of the request as it looks after the status change, for the bell
    modal. The signature storage path is never included; only has_signature."""
    snap = {k: v for k, v in row.items() if k not in SNAPSHOT_EXCLUDE}
    snap.update({
        "request_id": row.get("id"),
        "record_type": kind,
        "status": new_status,
        "updated_at": updated_at,
        "has_signature": bool(row.get("signature_path")),
        "email_verified": bool(row.get("requester_email")),
        "source": "status_update",
    })
    return snap


def notify_bell(kind, record_id, control_no, new_status, status_label, note, snapshot):
    """Create an admin-bell notification for a status change.
    push_notification never raises, so a bell problem can't fail the update."""
    message = f"{kind.title()} request {control_no} is now {status_label}."
    if note:
        message += f" Remark: {note}"

    push_notification(
        record_type=kind,
        record_id=record_id,
        control_no=control_no,
        title=f"Request {status_label}",
        message=message,
        request_snapshot=snapshot,
    )


@citizen_requests_bp.route("/api/requests", methods=["GET"])
@_staff_required
def list_citizen_requests():
    record_type = request.args.get("type", "").strip().lower()
    status = request.args.get("status", "").strip().upper()
    search = request.args.get("search", "").strip()
    limit = min(max(request.args.get("limit", 50, type=int), 1), 200)
    offset = max(request.args.get("offset", 0, type=int), 0)

    try:
        query = supabase.table(REQUEST_TABLE).select(
            "id, record_type, control_no, status, num_copies, purposes, "
            "requester_name, requester_relationship, requester_telephone, "
            "created_at, updated_at, "
            "child_firstname, child_surname, "
            "deceased_firstname, deceased_surname, "
            "husband_fullname, wife_maiden_name",
            count="exact",
        )

        if record_type in ("birth", "death", "marriage"):
            query = query.eq("record_type", record_type)
        if status in ALL_STATUSES:
            query = query.eq("status", status)
        if search:
            safe = search.replace(",", " ").replace("(", " ").replace(")", " ")
            query = query.or_(
                f"control_no.ilike.%{safe}%,requester_name.ilike.%{safe}%,"
                f"child_firstname.ilike.%{safe}%,child_surname.ilike.%{safe}%,"
                f"deceased_firstname.ilike.%{safe}%,deceased_surname.ilike.%{safe}%,"
                f"husband_fullname.ilike.%{safe}%,wife_maiden_name.ilike.%{safe}%"
            )

        res = (
            query.order("created_at", desc=True)
            .order("id", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )

        rows = res.data or []
        for r in rows:
            kind = (r.get("record_type") or "birth").lower()
            r["who"] = who(kind, r)
            st = (r.get("status") or "PENDING").upper()
            r["status_label"] = STATUS_LABELS.get(st, st)

        return jsonify({"total": res.count or 0, "requests": rows})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@citizen_requests_bp.route("/api/requests/<int:record_id>", methods=["GET"])
@_staff_required
def get_citizen_request_detail(record_id):
    try:
        rows = supabase.table(REQUEST_TABLE).select("*").eq("id", record_id).limit(1).execute().data
        if not rows:
            return jsonify({"error": "Request not found"}), 404
        row = rows[0]

        # Read signature_path BEFORE popping it.
        signature_path = row.pop("signature_path", None)
        has_signature = bool(signature_path)

        kind = (row.get("record_type") or "birth").lower()
        row["who"] = who(kind, row)
        st = (row.get("status") or "PENDING").upper()
        row["status_label"] = STATUS_LABELS.get(st, st)
        row["has_signature"] = has_signature
        row["signature_url"] = _signed_signature_url(signature_path)
        row["email_verified"] = bool(row.get("requester_email"))
        return jsonify(row)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@citizen_requests_bp.route("/api/requests/<int:record_id>/signature", methods=["GET"])
@_staff_required
def get_citizen_request_signature(record_id):
    """Serves the signature file for the admin modal's <img>. Session-gated,
    so the browser's login cookie is enough. The file is read from the
    private bucket."""
    try:
        rows = (
            supabase.table(REQUEST_TABLE)
            .select("signature_path")
            .eq("id", record_id)
            .limit(1)
            .execute()
            .data
        )
        path = rows[0].get("signature_path") if rows else None
        if not path:
            return jsonify({"error": "Signature not found"}), 404

        raw = supabase.storage.from_(SIGNATURE_BUCKET).download(path)
        ext = path.rsplit(".", 1)[-1].lower()
        resp = Response(raw, mimetype=SIGNATURE_MIME.get(ext, "application/octet-stream"))
        resp.headers["Cache-Control"] = "private, max-age=300"
        return resp
    except Exception as e:
        print(f"[citizen_requests] signature fetch failed for request {record_id}: {e}")
        traceback.print_exc()
        return jsonify({"error": "Could not load signature."}), 500


@citizen_requests_bp.route("/api/requests/<int:record_id>/status", methods=["PATCH"])
@_staff_required
def update_citizen_request_status(record_id):
    data = request.get_json(silent=True) or {}
    new_status = (data.get("status") or "").strip().upper()
    note = (data.get("note") or "").strip() or None

    notify_via = (data.get("notify_via") or "").strip().lower()
    if notify_via not in VALID_NOTIFY_VIA:
        notify_via = "email"

    if new_status not in ALL_STATUSES:
        return jsonify({"error": f"Invalid status. Must be one of: {', '.join(ALL_STATUSES)}"}), 400
    if new_status == "REJECTED" and not note:
        return jsonify({"error": "A remark is required when rejecting a request."}), 400

    try:
        rows = supabase.table(REQUEST_TABLE).select("*").eq("id", record_id).limit(1).execute().data
        if not rows:
            return jsonify({"error": "Request not found"}), 404
        row = rows[0]
        kind = (row.get("record_type") or "birth").lower()
        old_status = (row.get("status") or "PENDING").upper()

        if old_status == new_status:
            return jsonify({"error": f"Request is already {STATUS_LABELS.get(new_status, new_status)}."}), 400
        if old_status in ("COMPLETED", "REJECTED"):
            return jsonify({
                "error": f"This request is already {STATUS_LABELS.get(old_status, old_status)} and can no longer be changed."
            }), 400

        # 1. Save the new status. This is the only step that can fail the request.
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            supabase.table(REQUEST_TABLE).update({
                "status": new_status,
                "updated_at": now_iso,
            }).eq("id", record_id).execute()
        except Exception as db_err:
            print(f"[citizen_requests] Could not save status for request {record_id}: {db_err}")
            traceback.print_exc()
            return jsonify({"error": f"Could not save the new status: {db_err}"}), 500

        status_label = STATUS_LABELS.get(new_status, new_status)
        control_no = row.get("control_no")

        # 2. Admin bell notification (best-effort, never raises).
        if new_status in BELL_ON_STATUSES:
            snapshot = build_status_snapshot(kind, row, new_status, now_iso)
            notify_bell(
                kind=kind,
                record_id=record_id,
                control_no=control_no,
                new_status=new_status,
                status_label=status_label,
                note=note,
                snapshot=snapshot,
            )

        # 3. Citizen email (Completed / Rejected only), with a hard timeout.
        if new_status == "REJECTED":
            message = (
                f"We're sorry, but your {kind} certificate request "
                f"(Control No: {control_no}) has been rejected."
            )
            if note:
                message += f"\n\nRemark: {note}"
            message += "\n\nIf you have questions, please contact the Local Civil Registry."
        else:
            message = f"Your {kind} certificate request (Control No: {control_no}) is now: {status_label}."
            if note:
                message += f" Remark: {note}"

        requester_email = row.get("requester_email")
        status_triggers_email = new_status in NOTIFY_ON_STATUSES
        email_attempted = notify_via == "email" and status_triggers_email

        if email_attempted:
            email_sent = _send_email_notification(
                to_email=requester_email,
                subject=f"{kind.title()} Certificate Request — {status_label}",
                body=message,
            )
        else:
            email_sent = False

        email_skipped = notify_via != "none" and not status_triggers_email

        # 4. Audit log (best-effort).
        _safe_record_action(
            "REQUEST_STATUS_UPDATE",
            f"Updated {kind} request {control_no} from {old_status} to {new_status}",
            username=get_user(),
            meta={
                "record_id": record_id,
                "control_no": control_no,
                "old_status": old_status,
                "new_status": new_status,
                "note": note,
                "notify_via": notify_via,
                "email_sent": email_sent,
                "email_attempted": email_attempted,
                "email_skipped": email_skipped,
            },
            ip=request.remote_addr,
        )

        return jsonify({
            "ok": True,
            "record_id": record_id,
            "control_no": control_no,
            "old_status": old_status,
            "status": new_status,
            "status_label": status_label,
            "updated_at": now_iso,
            "notify_via": notify_via,
            "email_sent": email_sent,
            "email_attempted": email_attempted,
            "email_skipped": email_skipped,
            "notified_email": requester_email if email_sent else None,
            "message": f"Status updated to {status_label}",
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500