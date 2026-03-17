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
)
from webapp.services.csrf import ensure_csrf_token, validate_csrf
from webapp.services.session import login_required, get_session_user
from webapp.services.generation import generate_cards_from_pdf_bytes, normalize_text
from webapp.services.jobs import create_job, get_job, run_job, update_job
from webapp.services.companion import companion_reply
from webapp.services.markdown_render import render_markdown_safe

bp = Blueprint("app", __name__)


def _active_subject_id() -> int | None:
    sid = session.get("current_subject_id")
    return int(sid) if sid else None


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
    if u is None:
        return redirect(url_for("auth.login"))
    return redirect(url_for("app.dashboard"))


@bp.get("/dashboard")
@login_required
def dashboard():
    u = get_session_user()
    assert u is not None
    csrf = ensure_csrf_token()
    subjects = get_subjects(u.effective_user_id)
    return render_template(
        "app/dashboard.html",
        csrf_token=csrf,
        user=u,
        subjects=subjects,
        current_subject_id=_active_subject_id(),
    )


@bp.post("/subjects/select")
@login_required
def subjects_select():
    validate_csrf()
    sid = request.form.get("subject_id")
    session["current_subject_id"] = int(sid) if sid else None
    return redirect(url_for("app.subject"))


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
    flash(f"Subject '{name}' created.", "success")
    return redirect(url_for("app.subject"))


@bp.get("/subject")
@login_required
def subject():
    u = get_session_user()
    assert u is not None
    csrf = ensure_csrf_token()
    subject_id = _active_subject_id()
    subjects = get_subjects(u.effective_user_id)
    files = get_uploaded_files(u.effective_user_id, subject_id) if subject_id else []
    due_cards = get_due_cards(subject_id, limit=200) if subject_id else []
    due_cards = [c for c in due_cards if c.card_type != "multiple_choice"]
    return render_template(
        "app/subject.html",
        csrf_token=csrf,
        user=u,
        subjects=subjects,
        current_subject_id=subject_id,
        files=files,
        due_count=len(due_cards),
    )


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


@bp.get("/generate/progress/<job_id>")
@login_required
def generate_progress(job_id: str):
    u = get_session_user()
    assert u is not None
    csrf = ensure_csrf_token()
    return render_template("app/generate_progress.html", csrf_token=csrf, user=u, job_id=job_id)


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

    due_cards = get_due_cards(subject_id, limit=200)
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
    record_attempt(card_id, subject_id, u.effective_user_id, is_correct, quality)
    update_card_schedule(card_id, quality)

    session["show_answer"] = False
    session["srs_index"] = int(session.get("srs_index") or 0) + 1
    return redirect(url_for("app.study"))


def _quiz_items(subject_id: int, user_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT c.id, c.card_type, c.question, c.answer, c.source_pdf, c.page, c.subject_id, c.options
        FROM cards c
        JOIN subjects s ON c.subject_id = s.id
        WHERE c.subject_id = ?
          AND s.user_id = ?
          AND c.card_type IN ('short_answer', 'fill_in_blank', 'multiple_choice')
        ORDER BY c.id ASC
        """,
        (subject_id, user_id),
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

    items = _quiz_items(subject_id, u.effective_user_id)
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
    correct_answer = (request.form.get("correct_answer") or "").strip()
    card_type = request.form.get("card_type") or ""

    answers = session.get("quiz_answers") or {}
    answers[str(card_id)] = user_answer
    session["quiz_answers"] = answers

    if card_type == "multiple_choice":
        is_correct = user_answer == correct_answer
    else:
        is_correct = user_answer.lower() == correct_answer.lower()

    quality = 4 if is_correct else 2
    record_attempt(card_id, subject_id, u.effective_user_id, is_correct, quality)
    session["quiz_feedback"] = {"is_correct": is_correct, "correct_answer": correct_answer}
    return redirect(url_for("app.quiz"))


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
        WHERE c.subject_id = ? AND s.user_id = ?
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

