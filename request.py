"""Compatibility entry point for Render's ``request:app`` target."""

from app import app


if __name__ == "__main__":
    from config import DEBUG

    app.run(host="0.0.0.0", port=5001, debug=DEBUG)
