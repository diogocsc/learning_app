from __future__ import annotations

import secrets

from flask import session, request, abort


def ensure_csrf_token() -> str:
    tok = session.get("csrf_token")
    if not tok:
        tok = secrets.token_urlsafe(32)
        session["csrf_token"] = tok
    return tok


def validate_csrf() -> None:
    expected = session.get("csrf_token")
    provided = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    if not expected or not provided or provided != expected:
        abort(400, description="CSRF validation failed")

