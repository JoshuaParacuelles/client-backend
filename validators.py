import calendar
import datetime
import re
from zoneinfo import ZoneInfo

from flask import request

from config import KINDS

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
PH_LOCAL_RE = re.compile(r"^09\d{9}$")
PH_INTL_RE = re.compile(r"^\+639\d{9}$")
MONTH_NAMES = [m.lower() for m in calendar.month_name if m]
MAX_FIELD_LEN = 255


def clean(v):
    """Trim strings; empty string -> None (Postgres rejects '' for date columns)."""
    if isinstance(v, str):
        return v.strip() or None
    return v


def get_incoming_fields(kind):
    """Read submitted data from JSON (legacy) or multipart/form-data."""
    if (request.content_type or "").startswith("multipart/form-data"):
        return request.form.to_dict(), request.files.get("signature")
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None, None
    request_data = data.get(KINDS[kind]["key"], {}) or {}
    if not isinstance(request_data, dict):
        return None, None
    return request_data, None


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


def parse_month(v):
    s = (v or "").strip().lower().rstrip(".")
    if s.isdigit():
        return int(s) if 1 <= int(s) <= 12 else 0
    if len(s) >= 3:
        for i, name in enumerate(MONTH_NAMES, 1):
            if name.startswith(s):
                return i
    return 0


def validate_dates(kind, row):
    if kind == "marriage":
        return {}
    p = kind
    yk, mk, dk = f"{p}_year", f"{p}_month", f"{p}_date"
    today = datetime.datetime.now(ZoneInfo("Asia/Manila")).date()
    errs, year, month, day = {}, 0, 0, 0
    y, m, d = row.get(yk), row.get(mk), row.get(dk)
    if y:
        if re.fullmatch(r"\d{4}", y) and 1850 <= int(y) <= today.year:
            year = int(y)
        else:
            errs[yk] = f"Enter a valid 4-digit year (1850-{today.year})."
    if m:
        month = parse_month(m)
        if not month:
            errs[mk] = "Enter a valid month."
    if d:
        if re.fullmatch(r"\d{1,2}", d) and 1 <= int(d) <= 31:
            day = int(d)
        else:
            errs[dk] = "Enter a valid day (1-31)."
    if year and month and day:
        try:
            if datetime.date(year, month, day) > today:
                errs[dk] = "Date cannot be in the future."
        except ValueError:
            errs[dk] = "That date does not exist."
    elif year and month and datetime.date(year, month, 1) > today:
        errs[mk] = "Date cannot be in the future."
    return errs


def validate_lengths(row):
    return {k: "Too long." for k, v in row.items()
            if isinstance(v, str) and len(v) > MAX_FIELD_LEN}
