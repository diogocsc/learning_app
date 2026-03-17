from __future__ import annotations

from dataclasses import dataclass
from functools import wraps
from typing import Callable, TypeVar, cast

from flask import session, redirect, url_for, request

from db import get_connection

F = TypeVar("F", bound=Callable[..., object])


@dataclass(frozen=True)
class SessionUser:
    real_user_id: int
    effective_user_id: int
    real_username: str

    @property
    def is_impersonating(self) -> bool:
        return self.real_user_id != self.effective_user_id

    @property
    def is_admin(self) -> bool:
        return self.real_username == "admin"


def get_session_user() -> SessionUser | None:
    real_user_id = session.get("real_user_id")
    effective_user_id = session.get("effective_user_id")
    real_username = session.get("real_username")
    if not real_user_id or not effective_user_id:
        return None
    if not real_username:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT username FROM users WHERE id = ?", (int(real_user_id),))
        row = cur.fetchone()
        conn.close()
        real_username = row[0] if row else f"user_{int(real_user_id)}"
        session["real_username"] = real_username
    return SessionUser(
        real_user_id=int(real_user_id),
        effective_user_id=int(effective_user_id),
        real_username=str(real_username),
    )


def login_required(fn: F) -> F:
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if get_session_user() is None:
            return redirect(url_for("auth.login", next=request.path))
        return fn(*args, **kwargs)

    return cast(F, wrapper)


def admin_required(fn: F) -> F:
    @wraps(fn)
    def wrapper(*args, **kwargs):
        u = get_session_user()
        if u is None:
            return redirect(url_for("auth.login", next=request.path))
        if not u.is_admin:
            return redirect(url_for("app.dashboard"))
        return fn(*args, **kwargs)

    return cast(F, wrapper)

