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
    get_subjects,
    get_tracks,
    set_track_open_all,
    set_track_generation_defaults,
    set_open_all_for_all_tracks,
    gate_all_lessons_for_all_users,
)
from webapp.services.csrf import ensure_csrf_token, validate_csrf
from webapp.services.security import hash_password
from webapp.services.session import admin_required, get_session_user
from webapp.services.jobs import create_job, run_job, update_job
from webapp.services.track_generation import generate_track_from_subject_pdfs

bp = Blueprint("admin", __name__, url_prefix="/admin")


@bp.get("/tracks")
@admin_required
def tracks():
    u = get_session_user()
    assert u is not None
    csrf = ensure_csrf_token()
    subjects = get_subjects(u.effective_user_id)
    user_tracks = get_tracks(u.effective_user_id)
    return render_template(
        "admin/tracks.html",
        csrf_token=csrf,
        user=u,
        subjects=subjects,
        tracks=user_tracks,
    )


@bp.post("/tracks/create")
@admin_required
def tracks_create():
    validate_csrf()
    u = get_session_user()
    assert u is not None

    subject_id = int(request.form.get("subject_id") or 0)
    title = (request.form.get("title") or "").strip()
    num_lessons = int(request.form.get("num_lessons") or 6)
    cards_per_lesson = int(request.form.get("cards_per_lesson") or 12)

    if subject_id <= 0:
        flash("Select a subject.", "danger")
        return redirect(url_for("admin.tracks"))
    if num_lessons < 6 or num_lessons > 10:
        flash("num_lessons must be between 6 and 10.", "danger")
        return redirect(url_for("admin.tracks"))
    if cards_per_lesson < 1 or cards_per_lesson > 50:
        flash("cards_per_lesson must be between 1 and 50.", "danger")
        return redirect(url_for("admin.tracks"))

    # Store defaults used by normal users' "Generate track" button.
    set_track_generation_defaults(
        user_id=u.effective_user_id,
        subject_id=subject_id,
        num_lessons=num_lessons,
        cards_per_lesson=cards_per_lesson,
    )

    job = create_job()
    session["last_track_generate_job_id"] = job.job_id

    def do_work():
        def on_progress(current: int, total: int, message: str):
            update_job(job.job_id, current=int(current), total=int(total), message=message)

        track_id = generate_track_from_subject_pdfs(
            user_id=u.effective_user_id,
            subject_id=subject_id,
            title=title or "",
            num_lessons=num_lessons,
            cards_per_lesson=cards_per_lesson,
            on_progress=on_progress,
        )
        return {"track_id": track_id}

    run_job(job.job_id, do_work)
    return redirect(url_for("app.generate_track_progress", job_id=job.job_id))


@bp.post("/tracks/<int:track_id>/open-all")
@admin_required
def tracks_open_all(track_id: int):
    validate_csrf()
    u = get_session_user()
    assert u is not None
    ok = set_track_open_all(u.effective_user_id, track_id, open_all=True)
    if ok:
        admin_log(u.real_user_id, u.effective_user_id, f"Opened all lessons for track id={track_id}")
        flash("All lessons opened.", "success")
    else:
        flash("Track not found.", "danger")
    return redirect(url_for("admin.tracks"))


@bp.post("/tracks/open-all-all")
@admin_required
def tracks_open_all_all():
    validate_csrf()
    u = get_session_user()
    assert u is not None

    # Optional subject filter. If empty, applies to all subjects.
    subject_id_raw = request.form.get("subject_id") or ""
    subject_id = int(subject_id_raw) if subject_id_raw.strip().isdigit() else None

    changed = set_open_all_for_all_tracks(subject_id=subject_id)
    if changed:
        admin_log(u.real_user_id, u.real_user_id, f"Opened all lessons for all tracks{f' subject_id={subject_id}' if subject_id else ''}.")
        flash("Opened all lessons for all users.", "success")
    else:
        flash("No tracks found to update.", "warning")

    return redirect(url_for("admin.tracks"))


@bp.post("/tracks/gate-all-all")
@admin_required
def tracks_gate_all_all():
    validate_csrf()
    u = get_session_user()
    assert u is not None

    subject_id_raw = request.form.get("subject_id") or ""
    subject_id = int(subject_id_raw) if subject_id_raw.strip().isdigit() else None

    changed = gate_all_lessons_for_all_users(subject_id=subject_id)
    if changed:
        admin_log(u.real_user_id, u.real_user_id, f"Gated lessons for all users (subject_id={subject_id})")
        flash("Re-applied lesson gating for all users.", "success")
    else:
        flash("No tracks found to gate.", "warning")

    return redirect(url_for("admin.tracks"))


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

