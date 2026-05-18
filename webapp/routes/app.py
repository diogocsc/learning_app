from __future__ import annotations

import os
from pathlib import Path

from flask import Blueprint, render_template, request, redirect, url_for, flash, send_file, session, jsonify

from config import UPLOAD_DIR
from db import (
    add_subject,
    get_subjects,
    get_subject_id,
    insert_uploaded_file,
    get_uploaded_files,
    delete_uploaded_file_and_cards,
    update_excluded_pages,
    get_excluded_pages_map,
    get_due_cards,
    record_attempt,
    update_card_schedule,
    get_subject_stats,
    get_card_stats,
    delete_card,
    admin_log,
    get_connection,
    get_user_email,
    get_user_prefs,
    update_user_prefs,
    maybe_unlock_next_lesson,
    get_card_track_lesson,
    get_tracks,
    get_track,
    get_lessons,
    get_track_generation_defaults,
    count_user_tracks,
    delete_track_for_user,
)
from webapp.services.csrf import ensure_csrf_token, validate_csrf
from webapp.services.session import login_required, get_session_user
from webapp.services.generation import generate_cards_from_pdf_bytes, normalize_text
from webapp.services.jobs import create_job, get_job, run_job, update_job
from webapp.services.companion import companion_reply
from webapp.services.markdown_render import render_markdown_safe
from webapp.services.subjects import delete_subject_for_user
from webapp.services.emailer import send_achievement_email
from webapp.services.achievements_catalog import ACHIEVEMENTS
from webapp.services.exercise_grader import grade_exercise_answer
from webapp.services.track_generation import generate_track_from_subject_pdfs

bp = Blueprint("app", __name__)


def _active_subject_id() -> int | None:
    sid = session.get("current_subject_id")
    return int(sid) if sid else None


def _active_lesson_id() -> int | None:
    lid = session.get("active_lesson_id")
    return int(lid) if lid else None


def _companion_messages() -> list[dict]:
    msgs = session.get("companion_messages")
    if not isinstance(msgs, list):
        msgs = []
    # Ensure serializable shallow dicts
    cleaned = []
    for m in msgs:
        if isinstance(m, dict) and "role" in m and "content" in m:
            cleaned.append({"role": str(m["role"]), "content": str(m["content"])})
    session["companion_messages"] = cleaned
    return cleaned


@bp.get("/")
def index():
    u = get_session_user()
    csrf = ensure_csrf_token()
    return render_template("app/landing.html", csrf_token=csrf, user=u)


@bp.get("/dashboard")
@login_required
def dashboard():
    u = get_session_user()
    assert u is not None
    csrf = ensure_csrf_token()
    subjects = get_subjects(u.effective_user_id)
    subject_id = _active_subject_id()
    files = get_uploaded_files(u.effective_user_id, subject_id) if subject_id else []
    due_cards = get_due_cards(subject_id, limit=200, user_id=u.effective_user_id) if subject_id else []
    due_cards = [c for c in due_cards if c.card_type != "multiple_choice"]
    all_tracks = get_tracks(u.effective_user_id)
    tracks = [t for t in all_tracks if subject_id is None or t["subject_id"] == subject_id]
    # Keep the dashboard minimalist; the full list lives on `/tracks`.
    tracks = tracks[:6]
    return render_template(
        "app/workspace.html",
        csrf_token=csrf,
        user=u,
        subjects=subjects,
        current_subject_id=subject_id,
        files=files,
        due_count=len(due_cards),
        tracks=tracks,
    )


@bp.get("/tracks")
@login_required
def tracks():
    u = get_session_user()
    assert u is not None
    csrf = ensure_csrf_token()
    user_tracks = get_tracks(u.effective_user_id)
    return render_template("app/tracks.html", csrf_token=csrf, user=u, tracks=user_tracks)


@bp.get("/achievements")
@login_required
def achievements():
    u = get_session_user()
    assert u is not None
    csrf = ensure_csrf_token()
    return render_template("app/achievements.html", csrf_token=csrf, user=u)


@bp.post("/tracks/<int:track_id>/delete")
@login_required
def tracks_delete(track_id: int):
    validate_csrf()
    u = get_session_user()
    assert u is not None

    ok = delete_track_for_user(user_id=u.effective_user_id, track_id=track_id)
    if ok:
        # Clear gating state if user was viewing a lesson from this track.
        try:
            active_lesson_id = session.get("active_lesson_id")
            if active_lesson_id:
                # If active lesson belongs to deleted track, clear it.
                conn = get_connection()
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT 1
                    FROM lessons l
                    WHERE l.id = ?
                      AND l.track_id = ?
                    """,
                    (int(active_lesson_id), track_id),
                )
                allowed = cur.fetchone()
                conn.close()
                if allowed:
                    session.pop("active_lesson_id", None)
                    session.pop("srs_index", None)
                    session.pop("quiz_index", None)
        except Exception:
            pass

        flash("Track deleted.", "success")
    else:
        flash("Track not found or you do not have permission to delete it.", "danger")
    return redirect(url_for("app.tracks"))


@bp.get("/tracks/<int:track_id>")
@login_required
def track_view(track_id: int):
    u = get_session_user()
    assert u is not None
    csrf = ensure_csrf_token()
    t = get_track(u.effective_user_id, track_id)
    if not t:
        flash("Track not found.", "danger")
        return redirect(url_for("app.tracks"))
    lessons = get_lessons(u.effective_user_id, track_id)
    first_unlocked = next((l for l in lessons if l.get("is_unlocked")), None)
    first_unlocked_lesson_id = int(first_unlocked["id"]) if first_unlocked else None
    return render_template(
        "app/track_view.html",
        csrf_token=csrf,
        user=u,
        track=t,
        lessons=lessons,
        first_unlocked_lesson_id=first_unlocked_lesson_id,
    )


@bp.post("/tracks/<int:track_id>/lesson/<int:lesson_id>/study")
@login_required
def track_lesson_study(track_id: int, lesson_id: int):
    validate_csrf()
    u = get_session_user()
    assert u is not None
    return redirect(url_for("app.track_lesson_bite", track_id=track_id, lesson_id=lesson_id))


@bp.post("/tracks/<int:track_id>/lesson/<int:lesson_id>/quiz")
@login_required
def track_lesson_quiz(track_id: int, lesson_id: int):
    validate_csrf()
    u = get_session_user()
    assert u is not None
    return redirect(url_for("app.track_lesson_bite", track_id=track_id, lesson_id=lesson_id))


@bp.get("/tracks/<int:track_id>/lesson/<int:lesson_id>/bite")
@login_required
def track_lesson_bite(track_id: int, lesson_id: int):
    u = get_session_user()
    assert u is not None
    csrf = ensure_csrf_token()

    t = get_track(u.effective_user_id, track_id)
    if not t:
        flash("Track not found.", "danger")
        return redirect(url_for("app.tracks"))

    lessons = get_lessons(u.effective_user_id, track_id)
    lesson = next((l for l in lessons if int(l["id"]) == lesson_id), None)
    if not lesson or not lesson.get("is_unlocked"):
        flash("Lesson is locked.", "danger")
        return redirect(url_for("app.track_view", track_id=track_id))

    bite_md = lesson.get("bite_markdown") or lesson.get("brief") or ""
    bite_html = render_markdown_safe(bite_md)
    lesson_ctx = {**lesson, "bite_html": bite_html}
    return render_template("app/lesson_bite.html", csrf_token=csrf, user=u, track=t, lesson=lesson_ctx)


@bp.post("/tracks/<int:track_id>/lesson/<int:lesson_id>/start-study")
@login_required
def track_lesson_start_study(track_id: int, lesson_id: int):
    validate_csrf()
    u = get_session_user()
    assert u is not None

    t = get_track(u.effective_user_id, track_id)
    if not t:
        return redirect(url_for("app.tracks"))

    lessons = get_lessons(u.effective_user_id, track_id)
    lesson = next((l for l in lessons if int(l["id"]) == lesson_id), None)
    if not lesson or not lesson.get("is_unlocked"):
        flash("Lesson is locked.", "danger")
        return redirect(url_for("app.track_view", track_id=track_id))

    session["current_subject_id"] = int(t["subject_id"])
    session["active_lesson_id"] = int(lesson_id)
    session["srs_index"] = 0
    session["show_answer"] = False
    return redirect(url_for("app.study"))


@bp.post("/tracks/<int:track_id>/lesson/<int:lesson_id>/start-quiz")
@login_required
def track_lesson_start_quiz(track_id: int, lesson_id: int):
    validate_csrf()
    u = get_session_user()
    assert u is not None

    t = get_track(u.effective_user_id, track_id)
    if not t:
        return redirect(url_for("app.tracks"))

    lessons = get_lessons(u.effective_user_id, track_id)
    lesson = next((l for l in lessons if int(l["id"]) == lesson_id), None)
    if not lesson or not lesson.get("is_unlocked"):
        flash("Lesson is locked.", "danger")
        return redirect(url_for("app.track_view", track_id=track_id))

    session["current_subject_id"] = int(t["subject_id"])
    session["active_lesson_id"] = int(lesson_id)
    session["quiz_index"] = 0
    session.pop("quiz_feedback", None)
    return redirect(url_for("app.quiz"))


@bp.post("/subjects/select")
@login_required
def subjects_select():
    validate_csrf()
    sid = request.form.get("subject_id")
    session["current_subject_id"] = int(sid) if sid else None
    session.pop("active_lesson_id", None)
    return redirect(url_for("app.dashboard"))


@bp.post("/subjects/create")
@login_required
def subjects_create():
    validate_csrf()
    u = get_session_user()
    assert u is not None
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Please provide a subject name.", "danger")
        return redirect(url_for("app.dashboard"))
    add_subject(name, u.effective_user_id)
    if u.is_impersonating:
        admin_log(u.real_user_id, u.effective_user_id, f"Created subject '{name}'")
    # select it
    session["current_subject_id"] = get_subject_id(name, u.effective_user_id)
    session.pop("active_lesson_id", None)
    flash(f"Subject '{name}' created.", "success")
    return redirect(url_for("app.dashboard"))


@bp.post("/subjects/delete")
@login_required
def subjects_delete():
    validate_csrf()
    u = get_session_user()
    assert u is not None
    subject_id = int(request.form.get("subject_id") or 0)
    if subject_id <= 0:
        return redirect(url_for("app.dashboard"))

    ok = delete_subject_for_user(subject_id=subject_id, user_id=u.effective_user_id)
    if not ok:
        flash("Subject not found.", "danger")
        return redirect(url_for("app.dashboard"))

    # Clear active subject if it was deleted
    if _active_subject_id() == subject_id:
        session["current_subject_id"] = None
        session.pop("active_lesson_id", None)

    if u.is_impersonating:
        admin_log(u.real_user_id, u.effective_user_id, f"Deleted subject id={subject_id} and its data")

    flash("Subject deleted.", "success")
    return redirect(url_for("app.dashboard"))


@bp.get("/subject")
@login_required
def subject():
    # Compatibility route (old links)
    return redirect(url_for("app.dashboard"))


@bp.post("/uploads/add")
@login_required
def uploads_add():
    validate_csrf()
    u = get_session_user()
    assert u is not None
    subject_id = _active_subject_id()
    if subject_id is None:
        flash("Select a subject first.", "danger")
        return redirect(url_for("app.subject"))

    files = request.files.getlist("pdfs")
    if not files or not files[0].filename:
        flash("Please choose one or more PDFs.", "danger")
        return redirect(url_for("app.subject"))

    for f in files:
        if not f.filename.lower().endswith(".pdf"):
            continue
        pdf_name = Path(f.filename).name
        user_folder = UPLOAD_DIR / f"user_{u.effective_user_id}" / f"subject_{subject_id}"
        user_folder.mkdir(parents=True, exist_ok=True)
        stored_path = user_folder / pdf_name
        f.save(stored_path)
        insert_uploaded_file(u.effective_user_id, subject_id, pdf_name, str(stored_path))

    flash("Upload complete.", "success")
    return redirect(url_for("app.subject"))


@bp.post("/uploads/exclusions")
@login_required
def uploads_exclusions():
    validate_csrf()
    u = get_session_user()
    assert u is not None
    file_id = int(request.form.get("file_id") or 0)
    excluded = (request.form.get("excluded_pages") or "").strip()
    update_excluded_pages(file_id, excluded)
    flash("Saved excluded pages.", "success")
    return redirect(url_for("app.subject"))


@bp.get("/uploads/download/<int:file_id>")
@login_required
def uploads_download(file_id: int):
    u = get_session_user()
    assert u is not None
    subject_id = _active_subject_id()
    if subject_id is None:
        return redirect(url_for("app.subject"))
    files = get_uploaded_files(u.effective_user_id, subject_id)
    meta = next((x for x in files if x["id"] == file_id), None)
    if not meta:
        flash("File not found.", "danger")
        return redirect(url_for("app.subject"))
    return send_file(meta["stored_path"], as_attachment=True, download_name=meta["filename"], mimetype="application/pdf")


@bp.post("/uploads/delete")
@login_required
def uploads_delete():
    validate_csrf()
    u = get_session_user()
    assert u is not None
    file_id = int(request.form.get("file_id") or 0)
    deleted_path = delete_uploaded_file_and_cards(file_id, u.effective_user_id)
    if deleted_path and os.path.exists(deleted_path):
        try:
            os.remove(deleted_path)
        except OSError:
            pass
    if u.is_impersonating:
        admin_log(u.real_user_id, u.effective_user_id, f"Deleted file id={file_id} and its cards")
    flash("File deleted.", "success")
    return redirect(url_for("app.subject"))


@bp.post("/generate/from-upload")
@login_required
def generate_from_upload():
    validate_csrf()
    u = get_session_user()
    assert u is not None
    subject_id = _active_subject_id()
    if subject_id is None:
        flash("Select a subject first.", "danger")
        return redirect(url_for("app.subject"))

    file_id = int(request.form.get("file_id") or 0)
    max_cards = int(request.form.get("max_cards") or 50)

    files = get_uploaded_files(u.effective_user_id, subject_id)
    meta = next((x for x in files if x["id"] == file_id), None)
    if not meta:
        flash("File not found.", "danger")
        return redirect(url_for("app.subject"))

    job = create_job()
    session["last_generate_job_id"] = job.job_id

    def do_work():
        try:
            with open(meta["stored_path"], "rb") as f:
                file_bytes = f.read()
        except FileNotFoundError:
            raise RuntimeError("PDF file missing on disk.")

        def on_progress(current: int, total: int, message: str):
            update_job(job.job_id, current=int(current), total=int(total), message=message)

        added = generate_cards_from_pdf_bytes(
            user_id=u.effective_user_id,
            subject_id=subject_id,
            pdf_name=meta["filename"],
            file_bytes=file_bytes,
            file_id=file_id,
            max_cards=max_cards,
            on_progress=on_progress,
        )
        if u.is_impersonating:
            admin_log(u.real_user_id, u.effective_user_id, f"Generated {added} cards from {meta['filename']}")
        return {"added": added, "filename": meta["filename"]}

    run_job(job.job_id, do_work)
    return redirect(url_for("app.generate_progress", job_id=job.job_id))


@bp.post("/generate/track/from-all-pdfs")
@login_required
def generate_track_from_all_pdfs():
    validate_csrf()
    u = get_session_user()
    assert u is not None

    subject_id = _active_subject_id()
    if subject_id is None:
        flash("Select a subject first.", "danger")
        return redirect(url_for("app.subject"))

    files = get_uploaded_files(u.effective_user_id, subject_id)
    if not files:
        flash("Upload at least one PDF before generating a track.", "danger")
        return redirect(url_for("app.subject"))

    defaults = get_track_generation_defaults(u.effective_user_id, subject_id)
    num_lessons = int(defaults.get("num_lessons") or 6)
    cards_per_lesson = int(defaults.get("cards_per_lesson") or 12)

    next_track_num = count_user_tracks(u.effective_user_id, subject_id) + 1
    auto_title = f"Track {next_track_num}"

    job = create_job()
    session["last_generate_track_job_id"] = job.job_id

    def do_work():
        def on_progress(current: int, total: int, message: str):
            update_job(job.job_id, current=int(current), total=int(total), message=message)

        track_id = generate_track_from_subject_pdfs(
            user_id=u.effective_user_id,
            subject_id=subject_id,
            title=auto_title,
            num_lessons=num_lessons,
            cards_per_lesson=cards_per_lesson,
            on_progress=on_progress,
        )
        if u.is_impersonating:
            admin_log(u.real_user_id, u.effective_user_id, f"Generated track {auto_title} (id={track_id})")
        return {"track_id": track_id, "title": auto_title}

    run_job(job.job_id, do_work)
    return redirect(url_for("app.generate_track_progress", job_id=job.job_id))


@bp.get("/generate/progress/<job_id>")
@login_required
def generate_progress(job_id: str):
    u = get_session_user()
    assert u is not None
    csrf = ensure_csrf_token()
    return render_template("app/generate_progress.html", csrf_token=csrf, user=u, job_id=job_id)


@bp.get("/generate/track/progress/<job_id>")
@login_required
def generate_track_progress(job_id: str):
    u = get_session_user()
    assert u is not None
    csrf = ensure_csrf_token()
    return render_template("app/generate_track_progress.html", csrf_token=csrf, user=u, job_id=job_id)


@bp.get("/generate/status/<job_id>")
@login_required
def generate_status(job_id: str):
    st = get_job(job_id)
    if not st:
        return jsonify({"state": "error", "error": "Job not found"}), 404
    return jsonify(
        {
            "job_id": st.job_id,
            "state": st.state,
            "current": st.current,
            "total": st.total,
            "message": st.message,
            "result": st.result,
            "error": st.error,
        }
    )


@bp.get("/study")
@login_required
def study():
    u = get_session_user()
    assert u is not None
    csrf = ensure_csrf_token()
    subject_id = _active_subject_id()
    if subject_id is None:
        flash("Select a subject first.", "danger")
        return redirect(url_for("app.dashboard"))

    active_lesson_id = _active_lesson_id()
    due_cards = get_due_cards(
        subject_id,
        limit=200,
        user_id=u.effective_user_id,
        lesson_id=active_lesson_id,
    )
    due_cards = [c for c in due_cards if c.card_type != "multiple_choice"]
    if not due_cards:
        return render_template("app/study_done.html", csrf_token=csrf, user=u)

    idx = int(session.get("srs_index") or 0)
    if idx >= len(due_cards):
        idx = 0
    card = due_cards[idx]
    show_answer = bool(session.get("show_answer"))
    return render_template(
        "app/study.html",
        csrf_token=csrf,
        user=u,
        card=card,
        idx=idx,
        total=len(due_cards),
        show_answer=show_answer,
    )


@bp.post("/study/show-answer")
@login_required
def study_show_answer():
    validate_csrf()
    session["show_answer"] = True
    return redirect(url_for("app.study"))


@bp.post("/study/grade")
@login_required
def study_grade():
    validate_csrf()
    u = get_session_user()
    assert u is not None
    subject_id = _active_subject_id()
    if subject_id is None:
        return redirect(url_for("app.dashboard"))

    card_id = int(request.form.get("card_id") or 0)
    quality = int(request.form.get("quality") or 0)
    is_correct = quality >= 3

    active_lesson_id = _active_lesson_id()
    # Enforce lesson gating at write-time (prevents tampering with locked lesson card_ids).
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT 1
        FROM cards c
        JOIN subjects s ON c.subject_id = s.id
        LEFT JOIN tracks t ON c.track_id = t.id
        LEFT JOIN lessons l ON c.lesson_id = l.id
        WHERE c.id = ?
          AND s.user_id = ?
          AND c.subject_id = ?
          AND (? IS NULL OR c.lesson_id = ?)
          AND (
            c.track_id IS NULL
            OR t.open_all = 1
            OR l.lesson_index <= t.unlocked_lesson_index
          )
        """,
        (card_id, u.effective_user_id, subject_id, active_lesson_id, active_lesson_id),
    )
    allowed_row = cur.fetchone()
    conn.close()
    if not allowed_row:
        flash("This card is locked.", "danger")
        return redirect(url_for("app.study"))

    newly_earned = record_attempt(card_id, subject_id, u.effective_user_id, is_correct, quality)
    update_card_schedule(card_id, quality)

    # Unlock next lesson in any active track this card belongs to.
    try:
        track_lesson = get_card_track_lesson(card_id, u.effective_user_id)
        if track_lesson is not None:
            track_id, _lesson_id = track_lesson
            maybe_unlock_next_lesson(user_id=u.effective_user_id, track_id=track_id)
    except Exception:
        # Don't break study flow on unlock computation issues.
        pass

    _maybe_send_achievement_email(u, newly_earned)

    session["show_answer"] = False
    session["srs_index"] = int(session.get("srs_index") or 0) + 1
    return redirect(url_for("app.study"))


def _quiz_items(subject_id: int, user_id: int, *, lesson_id: int | None = None):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT c.id, c.card_type, c.question, c.answer, c.source_pdf, c.page, c.subject_id, c.options
        FROM cards c
        JOIN subjects s ON c.subject_id = s.id
        LEFT JOIN tracks t ON c.track_id = t.id
        LEFT JOIN lessons l ON c.lesson_id = l.id
        WHERE c.subject_id = ?
          AND s.user_id = ?
          AND c.card_type IN ('short_answer', 'fill_in_blank', 'multiple_choice', 'free_response', 'step_by_step', 'coding_task')
          AND (? IS NULL OR c.lesson_id = ?)
          AND (
            c.track_id IS NULL
            OR t.open_all = 1
            OR l.lesson_index <= t.unlocked_lesson_index
          )
        ORDER BY c.id ASC
        """,
        (subject_id, user_id, lesson_id, lesson_id),
    )
    rows = cur.fetchall()
    conn.close()
    import json as _json
    from models import QAItem

    items = []
    for r in rows:
        options = _json.loads(r[7]) if r[7] else None
        items.append(QAItem(id=r[0], card_type=r[1], question=r[2], answer=r[3], source_pdf=r[4], page=r[5], subject_id=r[6], options=options))
    return items


@bp.get("/quiz")
@login_required
def quiz():
    u = get_session_user()
    assert u is not None
    csrf = ensure_csrf_token()
    subject_id = _active_subject_id()
    if subject_id is None:
        flash("Select a subject first.", "danger")
        return redirect(url_for("app.dashboard"))

    active_lesson_id = _active_lesson_id()
    items = _quiz_items(subject_id, u.effective_user_id, lesson_id=active_lesson_id)
    if not items:
        return render_template("app/quiz_empty.html", csrf_token=csrf, user=u)

    idx = int(session.get("quiz_index") or 0)
    idx = max(0, min(idx, len(items) - 1))
    session["quiz_index"] = idx
    card = items[idx]
    prev_answer = (session.get("quiz_answers") or {}).get(str(card.id), "")
    feedback = session.pop("quiz_feedback", None)
    return render_template(
        "app/quiz.html",
        csrf_token=csrf,
        user=u,
        card=card,
        idx=idx,
        total=len(items),
        prev_answer=prev_answer,
        feedback=feedback,
    )


@bp.post("/quiz/nav")
@login_required
def quiz_nav():
    validate_csrf()
    direction = request.form.get("direction") or ""
    idx = int(session.get("quiz_index") or 0)
    if direction == "prev":
        idx -= 1
    elif direction == "next":
        idx += 1
    session["quiz_index"] = max(0, idx)
    return redirect(url_for("app.quiz"))


@bp.post("/quiz/check")
@login_required
def quiz_check():
    validate_csrf()
    u = get_session_user()
    assert u is not None
    subject_id = _active_subject_id()
    if subject_id is None:
        return redirect(url_for("app.dashboard"))

    card_id = int(request.form.get("card_id") or 0)
    user_answer = (request.form.get("user_answer") or "").strip()
    if not user_answer:
        return redirect(url_for("app.quiz"))

    # Persist answer in session (used to keep form state when navigating).
    answers = session.get("quiz_answers") or {}
    answers[str(card_id)] = user_answer
    session["quiz_answers"] = answers

    # Fetch card from DB to avoid relying on client-provided correct answers.
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT c.card_type, c.question, c.answer, c.options
        FROM cards c
        JOIN subjects s ON c.subject_id = s.id
        LEFT JOIN tracks t ON c.track_id = t.id
        LEFT JOIN lessons l ON c.lesson_id = l.id
        WHERE c.id = ?
          AND s.user_id = ?
          AND c.subject_id = ?
          AND (
            c.track_id IS NULL
            OR t.open_all = 1
            OR l.lesson_index <= t.unlocked_lesson_index
          )
        """,
        (card_id, u.effective_user_id, subject_id),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        flash("Question not found.", "danger")
        return redirect(url_for("app.quiz"))

    card_type = str(row[0])
    question = str(row[1] or "")
    correct_answer = str(row[2] or "")

    if card_type == "multiple_choice":
        is_correct = user_answer == correct_answer
        quality = 4 if is_correct else 2
        feedback_text = "Correct!" if is_correct else "Incorrect."
    elif card_type in ("short_answer", "fill_in_blank"):
        is_correct = user_answer.lower() == correct_answer.lower()
        quality = 4 if is_correct else 2
        feedback_text = "Correct!" if is_correct else "Incorrect."
    else:
        # Open-ended / coding-like exercises graded by LLM.
        try:
            grade = grade_exercise_answer(
                card_type=card_type,
                question=question,
                expected_answer=correct_answer,
                user_answer=user_answer,
                timeout_s=180,
            )
            is_correct = bool(grade.get("is_correct", False))
            quality = int(grade.get("quality", 0))
            quality = max(0, min(5, quality))
            feedback_text = str(grade.get("feedback") or "").strip()
        except Exception:
            is_correct = False
            quality = 0
            feedback_text = "Could not grade this answer automatically. Please try again."

    newly_earned = record_attempt(card_id, subject_id, u.effective_user_id, is_correct, quality)
    update_card_schedule(card_id, quality)

    # Unlock next lesson in any active track this card belongs to.
    try:
        track_lesson = get_card_track_lesson(card_id, u.effective_user_id)
        if track_lesson is not None:
            track_id, _lesson_id = track_lesson
            maybe_unlock_next_lesson(user_id=u.effective_user_id, track_id=track_id)
    except Exception:
        pass

    _maybe_send_achievement_email(u, newly_earned)

    session["quiz_feedback"] = {
        "is_correct": is_correct,
        "correct_answer": correct_answer if card_type in ("multiple_choice", "short_answer", "fill_in_blank") else None,
        "feedback_text": feedback_text,
    }
    return redirect(url_for("app.quiz"))


def _maybe_send_achievement_email(u, newly_earned: dict | None) -> None:
    try:
        prefs = get_user_prefs(u.effective_user_id)
        if not prefs.get("email_enabled", True):
            return
        to_email = get_user_email(u.effective_user_id)
        if not to_email:
            return
        earned_codes = set((newly_earned or {}).get("user", []) + (newly_earned or {}).get("subject", []))
        if not earned_codes:
            return
        catalog = {a["code"]: a for a in ACHIEVEMENTS}
        payload = [catalog[c] for c in earned_codes if c in catalog]
        if not payload:
            return
        send_achievement_email(to_email=to_email, username=u.real_username, achievements=payload)
    except Exception:
        return


@bp.get("/progress")
@login_required
def progress():
    u = get_session_user()
    assert u is not None
    csrf = ensure_csrf_token()
    subject_id = _active_subject_id()
    if subject_id is None:
        flash("Select a subject first.", "danger")
        return redirect(url_for("app.dashboard"))

    total, correct = get_subject_stats(subject_id, u.effective_user_id)
    accuracy = (correct / total * 100.0) if total else 0.0

    # list cards for subject (owned)
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT c.id, c.card_type, c.question, c.answer, c.source_pdf, c.page, c.subject_id, c.options
        FROM cards c
        JOIN subjects s ON c.subject_id = s.id
        LEFT JOIN tracks t ON c.track_id = t.id
        LEFT JOIN lessons l ON c.lesson_id = l.id
        WHERE c.subject_id = ? AND s.user_id = ?
          AND (
            c.track_id IS NULL
            OR t.open_all = 1
            OR l.lesson_index <= t.unlocked_lesson_index
          )
        ORDER BY c.id DESC
        """,
        (subject_id, u.effective_user_id),
    )
    rows = cur.fetchall()
    conn.close()
    import json as _json
    from models import QAItem

    cards = []
    for r in rows:
        options = _json.loads(r[7]) if r[7] else None
        cards.append(QAItem(id=r[0], card_type=r[1], question=r[2], answer=r[3], source_pdf=r[4], page=r[5], subject_id=r[6], options=options))

    stats_rows = []
    for c in cards:
        t, a = get_card_stats(c.id, u.effective_user_id)
        stats_rows.append({"card": c, "attempts": t, "correct": a, "accuracy": (a / t * 100.0) if t else None})

    return render_template(
        "app/progress.html",
        csrf_token=csrf,
        user=u,
        total_attempts=total,
        correct_attempts=correct,
        accuracy=accuracy,
        stats_rows=stats_rows,
    )


@bp.post("/cards/delete")
@login_required
def cards_delete():
    validate_csrf()
    u = get_session_user()
    assert u is not None
    card_id = int(request.form.get("card_id") or 0)
    ok = delete_card(card_id, u.effective_user_id)
    if ok and u.is_impersonating:
        admin_log(u.real_user_id, u.effective_user_id, f"Deleted card {card_id}")
    flash("Card deleted." if ok else "Could not delete card.", "success" if ok else "danger")
    return redirect(url_for("app.progress"))


@bp.get("/preferences")
@login_required
def preferences():
    u = get_session_user()
    assert u is not None
    csrf = ensure_csrf_token()
    prefs = get_user_prefs(u.effective_user_id)
    email = get_user_email(u.effective_user_id) or ""
    return render_template("app/preferences.html", csrf_token=csrf, user=u, prefs=prefs, email=email)


@bp.post("/preferences")
@login_required
def preferences_post():
    validate_csrf()
    u = get_session_user()
    assert u is not None
    email_enabled = bool(request.form.get("email_enabled"))
    weekly_email_enabled = bool(request.form.get("weekly_email_enabled"))
    weekly_day = int(request.form.get("weekly_email_day") or 1)
    weekly_hour = int(request.form.get("weekly_email_hour") or 9)
    update_user_prefs(
        user_id=u.effective_user_id,
        email_enabled=email_enabled,
        weekly_email_enabled=weekly_email_enabled,
        weekly_email_day=weekly_day,
        weekly_email_hour=weekly_hour,
    )
    flash("Preferences saved.", "success")
    return redirect(url_for("app.preferences"))


@bp.get("/companion")
@login_required
def companion():
    u = get_session_user()
    assert u is not None
    csrf = ensure_csrf_token()
    subject_id = _active_subject_id()
    if subject_id is None:
        flash("Select a subject first to use the companion.", "danger")
        return redirect(url_for("app.dashboard"))
    messages = _companion_messages()
    rendered = []
    for m in messages:
        if m.get("role") == "assistant":
            rendered.append({**m, "html": render_markdown_safe(m.get("content", ""))})
        else:
            rendered.append(m)
    return render_template("app/companion.html", csrf_token=csrf, user=u, messages=rendered)


@bp.post("/companion/send")
@login_required
def companion_send():
    validate_csrf()
    u = get_session_user()
    assert u is not None
    subject_id = _active_subject_id()
    if subject_id is None:
        return redirect(url_for("app.dashboard"))

    text = (request.form.get("message") or "").strip()
    if not text:
        return redirect(url_for("app.companion"))

    messages = _companion_messages()
    messages.append({"role": "user", "content": text})

    try:
        reply = companion_reply(
            user_id=u.effective_user_id,
            subject_id=subject_id,
            user_message=text,
            recent_messages=messages,
        )
    except Exception as e:  # noqa: BLE001
        reply = f"Sorry — I couldn’t generate a response right now. ({e})"

    messages.append({"role": "assistant", "content": reply})
    session["companion_messages"] = messages
    return redirect(url_for("app.companion"))


@bp.post("/companion/clear")
@login_required
def companion_clear():
    validate_csrf()
    session.pop("companion_messages", None)
    flash("Companion chat cleared.", "success")
    return redirect(url_for("app.companion"))

