from __future__ import annotations

import random
from datetime import datetime, timedelta

from flask import Blueprint, render_template, request, redirect, url_for, flash, session

from db import create_user, get_user_by_username
from webapp.services.security import hash_password, verify_password
from webapp.services.csrf import ensure_csrf_token, validate_csrf

bp = Blueprint("auth", __name__, url_prefix="/auth")


_CAPTCHA_CATEGORIES = {
    "animals": [("cat", "🐱"), ("dog", "🐶"), ("frog", "🐸"), ("monkey", "🐵"), ("panda", "🐼"), ("lion", "🦁")],
    "food": [("pizza", "🍕"), ("apple", "🍎"), ("banana", "🍌"), ("cake", "🍰"), ("ice cream", "🍨"), ("burger", "🍔")],
    "faces": [
        ("smiling face", "😊"),
        ("laughing face", "😂"),
        ("crying face", "😢"),
        ("angry face", "😠"),
        ("winking face", "😉"),
        ("surprised face", "😲"),
    ],
    "objects": [("car", "🚗"), ("airplane", "✈️"), ("book", "📚"), ("computer", "💻"), ("phone", "📱"), ("light bulb", "💡")],
}


def _captcha_new() -> None:
    category_name = random.choice(list(_CAPTCHA_CATEGORIES.keys()))
    options = _CAPTCHA_CATEGORIES[category_name]
    target_label, correct_emoji = random.choice(options)
    distractors = [e for (label, e) in options if e != correct_emoji]
    distractors = random.sample(distractors, k=min(3, len(distractors)))
    if len(distractors) < 3:
        others = [
            e
            for cat, items in _CAPTCHA_CATEGORIES.items()
            for (label, e) in items
            if e not in [correct_emoji] + distractors
        ]
        needed = 3 - len(distractors)
        distractors += random.sample(others, k=needed)

    choices = distractors + [correct_emoji]
    random.shuffle(choices)
    session["captcha_target_label"] = target_label
    session["captcha_correct_emoji"] = correct_emoji
    session["captcha_choices"] = choices


@bp.get("/login")
def login():
    csrf = ensure_csrf_token()
    return render_template("auth/login.html", csrf_token=csrf, next=request.args.get("next") or "/")


@bp.post("/login")
def login_post():
    validate_csrf()
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    next_url = request.form.get("next") or "/"

    if not username or not password:
        flash("Please enter both username and password.", "danger")
        return redirect(url_for("auth.login", next=next_url))

    user = get_user_by_username(username)
    if user is None:
        flash("User not found.", "danger")
        return redirect(url_for("auth.login", next=next_url))

    user_id, uname, pwd_hash = user
    if not verify_password(password, pwd_hash):
        flash("Incorrect password.", "danger")
        return redirect(url_for("auth.login", next=next_url))

    session["real_user_id"] = int(user_id)
    session["effective_user_id"] = int(user_id)
    session["real_username"] = str(uname)
    session.permanent = True
    flash("Logged in successfully.", "success")
    return redirect(next_url)


@bp.get("/register")
def register():
    csrf = ensure_csrf_token()
    if "captcha_target_label" not in session:
        _captcha_new()
    return render_template(
        "auth/register.html",
        csrf_token=csrf,
        captcha_target_label=session.get("captcha_target_label"),
        captcha_choices=session.get("captcha_choices") or [],
    )


@bp.post("/register")
def register_post():
    validate_csrf()
    # Simple session-based rate limit (similar spirit to Streamlit)
    now = datetime.utcnow()
    first = session.get("reg_rate_first")
    attempts = int(session.get("reg_rate_attempts") or 0)
    window = timedelta(minutes=10)
    max_attempts = 5
    if first:
        first_dt = datetime.fromisoformat(first)
        if now - first_dt > window:
            first_dt = now
            attempts = 0
    else:
        first_dt = now

    if attempts >= max_attempts:
        flash("Too many registration attempts. Please wait before trying again.", "danger")
        return redirect(url_for("auth.register"))

    attempts += 1
    session["reg_rate_first"] = first_dt.isoformat()
    session["reg_rate_attempts"] = attempts

    username = (request.form.get("username") or "").strip()
    pw1 = request.form.get("password") or ""
    pw2 = request.form.get("password2") or ""
    selected = request.form.get("captcha_selected") or ""

    if not username or not pw1:
        flash("Username and password are required.", "danger")
        return redirect(url_for("auth.register"))
    if pw1 != pw2:
        flash("Passwords do not match.", "danger")
        return redirect(url_for("auth.register"))
    if len(pw1) < 6:
        flash("Please use a password with at least 6 characters.", "danger")
        return redirect(url_for("auth.register"))

    correct = session.get("captcha_correct_emoji")
    if not selected or selected != correct:
        flash("Human verification failed. Please try again.", "danger")
        _captcha_new()
        return redirect(url_for("auth.register"))

    ok = create_user(username, hash_password(pw1))
    if not ok:
        flash("Username already exists. Please choose another one.", "danger")
        _captcha_new()
        return redirect(url_for("auth.register"))

    session["reg_rate_attempts"] = 0
    _captcha_new()
    flash("Account created! You can now log in.", "success")
    return redirect(url_for("auth.login"))


@bp.post("/logout")
def logout():
    validate_csrf()
    session.clear()
    return redirect(url_for("auth.login"))

