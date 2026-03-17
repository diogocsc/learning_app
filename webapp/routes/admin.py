from __future__ import annotations

import os

from flask import Blueprint, render_template, request, redirect, url_for, flash, session

from config import UPLOAD_DIR
from db import (
    get_all_users,
    update_user_username,
    update_user_password,
    delete_user,
    admin_log,
    get_admin_logs,
    get_connection,
)
from webapp.services.csrf import ensure_csrf_token, validate_csrf
from webapp.services.security import hash_password
from webapp.services.session import admin_required, get_session_user

bp = Blueprint("admin", __name__, url_prefix="/admin")


@bp.get("/users")
@admin_required
def users():
    u = get_session_user()
    assert u is not None
    csrf = ensure_csrf_token()
    users_list = get_all_users()
    logs = get_admin_logs(limit=200)
    return render_template("admin/users.html", csrf_token=csrf, user=u, users=users_list, logs=logs)


@bp.post("/users/rename")
@admin_required
def users_rename():
    validate_csrf()
    u = get_session_user()
    assert u is not None
    user_id = int(request.form.get("user_id") or 0)
    new_username = (request.form.get("new_username") or "").strip()
    if not new_username:
        flash("Username cannot be empty.", "danger")
        return redirect(url_for("admin.users"))
    ok = update_user_username(user_id, new_username)
    if not ok:
        flash("Username already exists.", "danger")
        return redirect(url_for("admin.users"))
    admin_log(u.real_user_id, user_id, f"Renamed user id={user_id} to '{new_username}'")
    flash("Username updated.", "success")
    return redirect(url_for("admin.users"))


@bp.post("/users/password")
@admin_required
def users_password():
    validate_csrf()
    u = get_session_user()
    assert u is not None
    user_id = int(request.form.get("user_id") or 0)
    new_pw = request.form.get("new_password") or ""
    if len(new_pw) < 6:
        flash("Password must be at least 6 characters.", "danger")
        return redirect(url_for("admin.users"))
    update_user_password(user_id, hash_password(new_pw))
    admin_log(u.real_user_id, user_id, "Admin changed user password")
    flash("Password updated.", "success")
    return redirect(url_for("admin.users"))


@bp.post("/users/impersonate")
@admin_required
def users_impersonate():
    validate_csrf()
    u = get_session_user()
    assert u is not None
    target_id = int(request.form.get("target_user_id") or 0)
    session["effective_user_id"] = target_id
    session.pop("current_subject_id", None)
    session.pop("srs_index", None)
    session.pop("quiz_index", None)
    admin_log(u.real_user_id, target_id, f"Started impersonation of user id={target_id}")
    flash("Impersonation enabled.", "warning")
    return redirect(url_for("app.dashboard"))


@bp.post("/users/stop-impersonate")
@admin_required
def users_stop_impersonate():
    validate_csrf()
    u = get_session_user()
    assert u is not None
    session["effective_user_id"] = u.real_user_id
    session.pop("current_subject_id", None)
    session.pop("srs_index", None)
    session.pop("quiz_index", None)
    admin_log(u.real_user_id, u.real_user_id, "Stopped impersonation")
    flash("Impersonation disabled.", "success")
    return redirect(url_for("app.dashboard"))


@bp.post("/users/delete")
@admin_required
def users_delete():
    validate_csrf()
    u = get_session_user()
    assert u is not None
    user_id = int(request.form.get("user_id") or 0)
    if user_id <= 0:
        return redirect(url_for("admin.users"))
    # prevent deleting admin
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT username FROM users WHERE id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    if row and row[0] == "admin":
        flash("Cannot delete admin user.", "danger")
        return redirect(url_for("admin.users"))

    # Remove physical uploaded files best-effort
    user_dir = UPLOAD_DIR / f"user_{user_id}"
    if user_dir.exists():
        for root, dirs, files in os.walk(user_dir, topdown=False):
            for name in files:
                try:
                    os.remove(os.path.join(root, name))
                except OSError:
                    pass
            for name in dirs:
                try:
                    os.rmdir(os.path.join(root, name))
                except OSError:
                    pass
        try:
            os.rmdir(user_dir)
        except OSError:
            pass

    delete_user(user_id)
    admin_log(u.real_user_id, user_id, "Deleted user account")
    flash("User deleted.", "success")
    return redirect(url_for("admin.users"))

