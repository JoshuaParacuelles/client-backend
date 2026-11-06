import base64
import uuid

from config import BUCKET, EXT, MAX_SIG_BYTES
from supabase_client import supabase


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
    """Legacy JSON path. Returns (bytes, mime) or (None, None) if none was sent."""
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
    """Multipart path. Returns (bytes, mime) or (None, None) if no file was chosen."""
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
