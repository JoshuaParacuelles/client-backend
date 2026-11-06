import datetime
import hmac
from functools import wraps

from flask import Blueprint, Response, jsonify, request

from config import ADMIN_API_KEY, BUCKET, KINDS, MIME
from supabase_client import supabase

admin_bp = Blueprint("admin", __name__)


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not ADMIN_API_KEY:
            return jsonify({"error": "Admin endpoints are disabled."}), 503
        # bytes compare (str compare_digest raises on non-ASCII input)
        supplied = request.headers.get("X-Admin-Key", "").encode("utf-8")
        if not hmac.compare_digest(supplied, ADMIN_API_KEY.encode("utf-8")):
            return jsonify({"error": "Unauthorized"}), 401
        return fn(*args, **kwargs)
    return wrapper


@admin_bp.get("/api/<string:record_type>/<int:record_id>/signature")
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
        resp = Response(raw, mimetype=MIME.get(path.rsplit(".", 1)[-1], "application/octet-stream"))
        resp.headers["Cache-Control"] = "private, max-age=300"
        return resp
    except Exception as e:
        print(f"[request] signature fetch failed: {e}")
        return jsonify({"error": "Could not load signature."}), 500


@admin_bp.get("/api/notifications")
@admin_required
def get_notifications():
    try:
        res = (supabase.table("notification").select("*")
               .order("created_at", desc=True).limit(100).execute())
        return jsonify(res.data)
    except Exception as e:
        print(f"[request] notifications failed: {e}")
        return jsonify({"error": "Could not load notifications."}), 500


@admin_bp.get("/api/notifications/unread-count")
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


@admin_bp.patch("/api/notifications/<int:notif_id>/read")
@admin_required
def mark_read(notif_id):
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400
    read_by = data.get("read_by")
    try:
        supabase.table("notification").update(_read_patch(read_by)).eq("id", notif_id).execute()
        return jsonify({"ok": True})
    except Exception as e:
        print(f"[request] mark_read failed: {e}")
        return jsonify({"error": "Could not update notification."}), 500


@admin_bp.patch("/api/notifications/mark-all-read")
@admin_required
def mark_all_read():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400
    read_by = data.get("read_by")
    try:
        supabase.table("notification").update(_read_patch(read_by)).eq("is_read", False).execute()
        return jsonify({"ok": True})
    except Exception as e:
        print(f"[request] mark_all_read failed: {e}")
        return jsonify({"error": "Could not update notifications."}), 500
