# backend/

## Purpose
This folder contains the Flask backend for requests, email verification, feedback reports, signatures, validation, and the Supabase connection.

## Files
- `app.py` — Sets up Flask, CORS, proxy handling, rate limits, error responses, and route blueprints.
- `request.py` — Compatibility entry point that imports and runs the Flask app.
- `config.py` — Holds shared settings, record fields, status labels, allowed origins, and signature/feedback image size settings.
- `email_verification.py` — Sends and confirms email codes and provides the one-time verification token used during submission.
- `extensions.py` — Creates and exports the Flask-Limiter instance.
- `routes/feedback.py` — Validates and stores anonymous feedback reports and optional images.
- `http_utils.py` — Exports `client_ip()`, which returns the client IP from the request.
- `notifications.py` — Builds request details and creates a notification for a new request.
- `signatures.py` — Checks, decodes, reads, and stores signature files in Supabase.
- `supabase_client.py` — Creates and exports the Supabase client using environment settings.
- `validators.py` — Reads, cleans, and checks submitted fields and dates.
- `apply_legal_patch.py` — A script that applies preset edits only when the expected source text matches.
- `requirements.txt` — Lists the Python packages and versions used by the backend.
- `.env` — Local environment settings. Secret values are not listed here.
- `.gitignore` — Tells Git to ignore Python cache files, virtual environments, and `.env`.
- `request.py.bak` — Backup of an older, single-file backend with request, tracking, signature, notification, and admin code.
- `README.md` — This file.

## Dependencies
`app.py` registers email verification and route blueprints. The route files use helpers from config, validation, signatures, notifications, rate limits, and Supabase.

Run the feedback table and row-level-security statements in `../sql.sql` before enabling feedback submissions. The route uses the existing private `signatures` storage bucket under `feedback/`; the Flask service role performs all writes. Configure `RATELIMIT_STORAGE_URI` with shared Redis in multi-worker or multi-instance deployments so feedback limits are shared.

## Notes
`request.py.bak` contains older code that overlaps with the current separate modules; it is marked as a backup. Python creates `__pycache__/` automatically, and it does not need to be tracked in Git.
