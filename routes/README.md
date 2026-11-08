# backend/routes/

## Purpose
This folder contains the Flask routes for admin tools and public request actions.

## Files
- `__init__.py` — Marks this folder as a Python package for HTTP route blueprints.
- `admin.py` — Exports `admin_bp`. Its protected routes return signatures and notifications, count unread notifications, and mark notifications as read.
- `requests.py` — Exports `requests_bp`. Its routes handle health checks, birth/death/marriage submissions, and request tracking.
- `README.md` — This file.

## Dependencies
`app.py` registers both blueprints. `requests.py` uses verification, validation, signatures, notifications, rate limits, and Supabase. `admin.py` uses config and Supabase.

## Notes
Each route's URL and HTTP method are shown by the Flask decorators in its Python file.
