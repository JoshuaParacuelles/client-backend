import logging
import uuid
from urllib.parse import urlsplit, urlunsplit

from flask import Blueprint, jsonify, request

from config import MAX_FEEDBACK_FILE_BYTES
from extensions import limiter
from supabase_client import supabase

feedback_bp = Blueprint("feedback", __name__)
logger = logging.getLogger(__name__)

ALLOWED_TYPES = {
    "Bug Report",
    "Feature Suggestion",
    "General Feedback",
    "Content Error",
}
ALLOWED_IMAGES = {
    "image/png": ("png", b"\x89PNG\r\n\x1a\n"),
    "image/jpeg": ("jpg", b"\xff\xd8\xff"),
}
MAX_DESCRIPTION_CHARS = 500
STORAGE_BUCKET = "signatures"


def _read_image(field_name):
    uploaded = request.files.get(field_name)
    if uploaded is None or not uploaded.filename:
        return None

    data = uploaded.read(MAX_FEEDBACK_FILE_BYTES + 1)
    if not data:
        raise ValueError(f"{field_name} must not be empty.")
    if len(data) > MAX_FEEDBACK_FILE_BYTES:
        raise OverflowError(f"{field_name} must be 5 MB or smaller.")

    mime = uploaded.mimetype
    if mime == "image/webp":
        if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
            raise ValueError(f"{field_name} must be a valid PNG, JPG, or WEBP image.")
        return data, "webp", "image/webp"

    image = ALLOWED_IMAGES.get(mime)
    if image is None or not data.startswith(image[1]):
        raise ValueError(f"{field_name} must be a valid PNG, JPG, or WEBP image.")
    return data, image[0], mime


def _safe_page_url(value):
    try:
        parsed = urlsplit(value[:2048].strip())
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.netloc
            or parsed.username
            or parsed.password
        ):
            return ""
        host = parsed.hostname
        if not host:
            return ""
        port = f":{parsed.port}" if parsed.port else ""
        return urlunsplit((parsed.scheme, f"{host}{port}", parsed.path, "", ""))[:500]
    except ValueError:
        return ""


def _remove_uploaded_files(paths):
    if not paths:
        return
    try:
        supabase.storage.from_(STORAGE_BUCKET).remove(paths)
    except Exception:
        logger.exception("Could not remove feedback files after a failed submission")


@feedback_bp.post("/api/feedback")
@limiter.limit("5 per minute; 20 per hour")
def submit_feedback():
    issue_type = (request.form.get("issue_type") or "").strip()
    description = (request.form.get("description") or "").strip()
    if issue_type not in ALLOWED_TYPES:
        return jsonify(error="Invalid issue type."), 400
    if not description or len(description) > MAX_DESCRIPTION_CHARS:
        return jsonify(
            error=f"Description is required and must be {MAX_DESCRIPTION_CHARS} characters or fewer."
        ), 400

    try:
        screenshot = _read_image("screenshot")
        attachment = _read_image("attachment")
    except OverflowError as error:
        return jsonify(error=str(error)), 413
    except ValueError as error:
        return jsonify(error=str(error)), 400

    report_id = str(uuid.uuid4())
    paths = []
    report_paths = {}
    try:
        for field_name, image in (("screenshot", screenshot), ("attachment", attachment)):
            if image is None:
                continue
            data, extension, mime = image
            path = f"feedback/{report_id}/{field_name}.{extension}"
            paths.append(path)
            supabase.storage.from_(STORAGE_BUCKET).upload(
                path,
                data,
                {"content-type": mime},
            )
            report_paths[f"{field_name}_path"] = path

        user_agent = (request.form.get("user_agent") or "")[:500]
        supabase.table("feedback_reports").insert({
            "id": report_id,
            "issue_type": issue_type,
            "description": description,
            "page_url": _safe_page_url(request.form.get("page_url") or ""),
            "user_agent": user_agent,
            "screenshot_path": report_paths.get("screenshot_path"),
            "attachment_path": report_paths.get("attachment_path"),
            "status": "open",
        }).execute()
    except Exception:
        _remove_uploaded_files(paths)
        logger.exception("Feedback submission failed")
        return jsonify(error="We couldn't submit your report. Please try again."), 500

    return jsonify(id=report_id, message="Report submitted."), 201
