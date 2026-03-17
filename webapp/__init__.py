from __future__ import annotations

import os
from datetime import timedelta

from flask import Flask, render_template

from db import init_db
from .routes.auth import bp as auth_bp
from .routes.app import bp as app_bp
from .routes.admin import bp as admin_bp


def create_app() -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")

    # Config
    app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=14)
    # Upload size limit (bytes). Override with MAX_UPLOAD_BYTES.
    # Default bumped for PDF-heavy consumer usage.
    app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_UPLOAD_BYTES", str(250 * 1024 * 1024)))  # 250MB

    # Ensure DB exists and admin user is present
    init_db()

    # Blueprints
    app.register_blueprint(auth_bp)
    app.register_blueprint(app_bp)
    app.register_blueprint(admin_bp)

    @app.errorhandler(413)
    def request_entity_too_large(_e):
        # Render a friendly page with next steps.
        max_mb = int(app.config.get("MAX_CONTENT_LENGTH", 0) / (1024 * 1024)) if app.config.get("MAX_CONTENT_LENGTH") else 0
        return render_template("errors/413.html", max_mb=max_mb), 413

    return app

