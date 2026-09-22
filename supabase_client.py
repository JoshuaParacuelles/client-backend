# backend/supabase_client.py  (request-form project)
import os
from supabase import create_client, Client

url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_KEY")

if not url or not key:
    raise ValueError(
        "SUPABASE_URL and SUPABASE_KEY must be set. "
        f"URL set: {bool(url)}, KEY set: {bool(key)}. "
        "Make sure load_dotenv() runs BEFORE this module is imported."
    )

supabase: Client = create_client(url, key)