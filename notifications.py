from config import COMMON, KINDS


def subject_of(kind, row):
    if kind == "marriage":
        return f"{row.get('husband_fullname') or ''} & {row.get('wife_maiden_name') or ''}".strip()
    p = "child" if kind == "birth" else "deceased"
    return f"{row.get(p + '_firstname') or ''} {row.get(p + '_surname') or ''}".strip()


def mask_name(text):
    """'Juan Dela Cruz' -> 'J*** D*** C***' (privacy-safe display)."""
    return " ".join((w[0] + "***") if len(w) > 1 else w for w in (text or "").split())


def build_request_snapshot(kind, row):
    """Full citizen submission for the admin notification, excluding signature path."""
    cfg = KINDS[kind]
    snapshot = {field: row.get(field) for field in cfg["fields"] + COMMON}
    snapshot.update({
        "request_id": row.get("id"),
        "control_no": row.get("control_no"),
        "record_type": kind,
        "status": (row.get("status") or "PENDING").upper(),
        "created_at": row.get("created_at"),
        "has_signature": bool(row.get("signature_path")),
        "email_verified": True,
        "source": "online_request",
    })
    return snapshot


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
            "request_snapshot": build_request_snapshot(kind, row),
            "is_read": False,
        }).execute()
    except Exception as e:
        print(f"[request] notification insert failed (ignored): {e}")
        return None
