import logging
import struct
import uuid
import warnings
from io import BytesIO
from urllib.parse import urlsplit, urlunsplit

from flask import Blueprint, jsonify, request
from PIL import Image

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
    "Other Issue",
}
IMAGE_FORMATS = {
    "PNG": ("png", "image/png"),
    "JPEG": ("jpg", "image/jpeg"),
    "WEBP": ("webp", "image/webp"),
}
MAX_DESCRIPTION_CHARS = 500
MAX_OTHER_TYPE_CHARS = 100
STORAGE_BUCKET = "feedback-attachments"


def _read_image(field_name):
    uploaded = request.files.get(field_name)
    if uploaded is None or not uploaded.filename:
        return None

    data = uploaded.read(MAX_FEEDBACK_FILE_BYTES + 1)
    if not data:
        raise ValueError(f"{field_name} must not be empty.")
    if len(data) > MAX_FEEDBACK_FILE_BYTES:
        raise OverflowError(f"{field_name} must be 5 MB or smaller.")

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image = Image.open(BytesIO(data))
            image_format = image.format
            image.verify()
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
        ValueError,
        SyntaxError,
        EOFError,
        struct.error,
    ) as error:
        raise ValueError(f"{field_name} must be a valid PNG, JPG, or WEBP image.") from error

    image_info = IMAGE_FORMATS.get(image_format)
    if image_info is None:
        raise ValueError(f"{field_name} must be a valid PNG, JPG, or WEBP image.")
    extension, mime = image_info
    return data, extension, mime


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


@feedback_bp.post("/api/feedback")
@limiter.limit("5 per minute; 20 per hour")
def submit_feedback():
    issue_type = (request.form.get("issue_type") or "").strip()
    issue_subtype = (request.form.get("issue_subtype") or "").strip()
    description = (request.form.get("description") or "").strip()
    if issue_type not in ALLOWED_TYPES:
        return jsonify(error="Invalid issue type."), 400
    if issue_type == "Other Issue":
        if not issue_subtype:
            return jsonify(error="Please specify the issue type."), 400
        if len(issue_subtype) > MAX_OTHER_TYPE_CHARS:
            return jsonify(
                error=f"The issue type must be {MAX_OTHER_TYPE_CHARS} characters or fewer."
            ), 400
    else:
        issue_subtype = None
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
    report_paths = {}
    image_warnings = []
    for field_name, image in (("screenshot", screenshot), ("attachment", attachment)):
        if image is None:
            continue
        data, extension, mime = image
        path = f"{report_id}/{field_name}.{extension}"
        try:
            supabase.storage.from_(STORAGE_BUCKET).upload(
                path,
                data,
                {"content-type": mime},
            )
            report_paths[f"{field_name}_path"] = path
        except Exception:
            logger.exception("Could not upload feedback %s for report %s", field_name, report_id)
            image_warnings.append(field_name)

    try:
        supabase.table("feedback_reports").insert({
            "id": report_id,
            "issue_type": issue_type,
            "issue_subtype": issue_subtype,
            "description": description,
            "page_url": _safe_page_url(request.form.get("page_url") or ""),
            "user_agent": (request.form.get("user_agent") or "")[:500],
            "screenshot_path": report_paths.get("screenshot_path"),
            "attachment_path": report_paths.get("attachment_path"),
            "status": "open",
        }).execute()
    except Exception:
        logger.exception("Could not save feedback report %s", report_id)
        return jsonify(
            error="Your report could not be saved to the feedback database. Please try again later."
        ), 500

    response = {"id": report_id, "message": "Report submitted."}
    if image_warnings:
        response["warning"] = (
            "Your report was saved, but the "
            + " and ".join(image_warnings)
            + " could not be uploaded."
        )
    return jsonify(response), 201
