import os
from supabase import create_client, Client

url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")  # server-only; NEVER expose to the client

if not url or not key:
    raise RuntimeError(
        "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set "
        "(load_dotenv() must run before this module is imported)."
    )

supabase: Client = create_client(url, key)