import os
import re

BUCKET = "signatures"
MAX_SIG_BYTES = 2 * 1024 * 1024
MAX_FEEDBACK_FILE_BYTES = 5 * 1024 * 1024
EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "application/pdf": "pdf"}
MIME = {v: k for k, v in EXT.items()}

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
ALWAYS_REQUIRED = ["requester_name", "requester_relationship", "requester_address", "requester_email"]

STATUS_ORDER = ["PENDING", "PROCESSING", "COMPLETED"]
STATUS_LABELS = {
    "PENDING": "Pending Review",
    "PROCESSING": "Being Processed",
    "COMPLETED": "Completed",
    "REJECTED": "Rejected",
}

ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
ORIGIN_REGEX = os.environ.get("ALLOWED_ORIGIN_REGEX", "").strip()
IS_PROD = os.environ.get("APP_ENV") == "production"
if IS_PROD and ("*" in ALLOWED_ORIGINS or not ALLOWED_ORIGINS):
    raise RuntimeError("Set ALLOWED_ORIGINS to your real frontend origin(s) in production.")

ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY")
DEBUG = os.environ.get("FLASK_DEBUG") == "1"
CORS_ORIGINS = list(ALLOWED_ORIGINS)
if ORIGIN_REGEX:
    CORS_ORIGINS.append(re.compile(ORIGIN_REGEX))
