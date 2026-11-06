from dotenv import load_dotenv

load_dotenv()  # must run BEFORE importing modules that initialize Supabase

from flask import Flask, jsonify
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix

from config import CORS_ORIGINS, DEBUG, MAX_SIG_BYTES
from extensions import limiter

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)  # one proxy hop (Render)
limiter.init_app(app)
app.config["MAX_CONTENT_LENGTH"] = MAX_SIG_BYTES + 512 * 1024

CORS(app, resources={r"/api/*": {"origins": CORS_ORIGINS}}, expose_headers=["Retry-After"])


@app.errorhandler(413)
def too_large(_e):
    return jsonify({"error": "Upload is too large. The signature file must be 2 MB or smaller."}), 413


@app.errorhandler(429)
def rate_limit_hit(_e):
    return jsonify({"error": "Too many requests. Please slow down and try again shortly."}), 429


@app.after_request
def ensure_retry_after(response):
    if response.status_code == 429 and "Retry-After" not in response.headers:
        response.headers["Retry-After"] = "60"
    return response


from email_verification import verification_bp
from routes.admin import admin_bp
from routes.requests import requests_bp

app.register_blueprint(verification_bp)  # /api/verify/send, /api/verify/confirm
app.register_blueprint(requests_bp)
app.register_blueprint(admin_bp)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=DEBUG)
