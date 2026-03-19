# db.py
import os
import re
import sqlite3
from typing import List, Optional, Tuple, Dict
from pathlib import Path
from datetime import date, datetime, timedelta
import json

import bcrypt

from models import QAItem

# Project root = directory containing this file
PROJECT_ROOT = Path(__file__).resolve().parent

# DB in ./data/assistant.db (can be overridden with APP_DB_PATH)
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "assistant.db"
DB_PATH = Path(os.getenv("APP_DB_PATH", DEFAULT_DB_PATH))

DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def get_connection():
    # check_same_thread=False allows use with Streamlit
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def init_db():
    conn = get_connection()
    cursor = conn.cursor()

    # Users
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            email TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        """
    )

    # Backfill schema if DB existed before adding `email`
    cursor.execute("PRAGMA table_info(users)")
    cols = {r[1] for r in cursor.fetchall()}
    if "email" not in cols:
        cursor.execute("ALTER TABLE users ADD COLUMN email TEXT;")

    # Ensure default admin user exists
    cursor.execute("SELECT id FROM users WHERE username = 'admin'")
    if cursor.fetchone() is None:
        pw_hash = bcrypt.hashpw("admin123".encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        cursor.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            ("admin", pw_hash),
        )

    # Subjects (per user)
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS subjects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            UNIQUE(name, user_id),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        """
    )

    # Tracks & lessons (curriculum gating)
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            subject_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            num_lessons INTEGER NOT NULL,
            cards_per_lesson INTEGER NOT NULL,
            unlocked_lesson_index INTEGER NOT NULL DEFAULT 0,
            open_all INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(subject_id) REFERENCES subjects(id)
        );
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS lessons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            track_id INTEGER NOT NULL,
            lesson_index INTEGER NOT NULL,
            title TEXT NOT NULL,
            objectives TEXT NOT NULL DEFAULT '[]', -- JSON encoded list[str]
            anchor_queries TEXT NOT NULL DEFAULT '[]', -- JSON encoded list[str]
            brief TEXT,
            bite_markdown TEXT,
            UNIQUE(track_id, lesson_index),
            FOREIGN KEY(track_id) REFERENCES tracks(id)
        );
        """
    )

    # Cards with SRS fields
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_type TEXT NOT NULL,
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            source_pdf TEXT NOT NULL,
            page INTEGER NOT NULL,
            subject_id INTEGER NOT NULL,
            track_id INTEGER,
            lesson_id INTEGER,
            options TEXT, -- JSON list for MCQ, NULL otherwise

            -- Spaced repetition fields
            ef REAL NOT NULL DEFAULT 2.5, -- easiness factor
            interval INTEGER NOT NULL DEFAULT 0, -- days
            repetitions INTEGER NOT NULL DEFAULT 0,
            due_date TEXT NOT NULL DEFAULT (DATE('now')),
            last_review TEXT,
            lapse_count INTEGER NOT NULL DEFAULT 0,

            FOREIGN KEY(subject_id) REFERENCES subjects(id)
        );
        """
    )

    # Backfill `cards.track_id` / `cards.lesson_id` if DB predates them.
    cursor.execute("PRAGMA table_info(cards)")
    cols = {r[1] for r in cursor.fetchall()}
    if "track_id" not in cols:
        cursor.execute("ALTER TABLE cards ADD COLUMN track_id INTEGER;")
    if "lesson_id" not in cols:
        cursor.execute("ALTER TABLE cards ADD COLUMN lesson_id INTEGER;")

    # Backfill `lessons.bite_markdown` if DB predates it.
    cursor.execute("PRAGMA table_info(lessons)")
    lesson_cols = {r[1] for r in cursor.fetchall()}
    if "bite_markdown" not in lesson_cols:
        cursor.execute("ALTER TABLE lessons ADD COLUMN bite_markdown TEXT;")

    # Attempts history (for progress stats), per user
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS card_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_id INTEGER NOT NULL,
            subject_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            is_correct INTEGER NOT NULL,
            quality INTEGER NOT NULL, -- 0-5
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(card_id) REFERENCES cards(id),
            FOREIGN KEY(subject_id) REFERENCES subjects(id),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        """
    )

    # Uploaded files metadata
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS uploaded_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            subject_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            stored_path TEXT NOT NULL,
            uploaded_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            excluded_pages TEXT DEFAULT '',
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(subject_id) REFERENCES subjects(id)
        );
        """
    )

    # Admin action logs (impersonation, destructive actions, etc.)
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER NOT NULL,
            target_user_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(admin_id) REFERENCES users(id),
            FOREIGN KEY(target_user_id) REFERENCES users(id)
        );
        """
    )

    # ---------- Gamification ----------
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS user_stats (
            user_id INTEGER PRIMARY KEY,
            xp INTEGER NOT NULL DEFAULT 0,
            level INTEGER NOT NULL DEFAULT 1,
            current_streak INTEGER NOT NULL DEFAULT 0,
            best_streak INTEGER NOT NULL DEFAULT 0,
            last_streak_date TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS subject_stats (
            user_id INTEGER NOT NULL,
            subject_id INTEGER NOT NULL,
            xp INTEGER NOT NULL DEFAULT 0,
            level INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (user_id, subject_id),
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(subject_id) REFERENCES subjects(id)
        );
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_progress (
            user_id INTEGER NOT NULL,
            day TEXT NOT NULL, -- YYYY-MM-DD
            attempts INTEGER NOT NULL DEFAULT 0,
            correct INTEGER NOT NULL DEFAULT 0,
            xp INTEGER NOT NULL DEFAULT 0,
            streak_counted INTEGER NOT NULL DEFAULT 0, -- 0/1
            PRIMARY KEY (user_id, day),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        """
    )

    # Achievements (badges)
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS user_achievements (
            user_id INTEGER NOT NULL,
            code TEXT NOT NULL,
            earned_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, code),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS subject_achievements (
            user_id INTEGER NOT NULL,
            subject_id INTEGER NOT NULL,
            code TEXT NOT NULL,
            earned_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, subject_id, code),
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(subject_id) REFERENCES subjects(id)
        );
        """
    )

    # Default track generation settings used by normal users
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS track_generation_defaults (
            user_id INTEGER NOT NULL,
            subject_id INTEGER NOT NULL,
            num_lessons INTEGER NOT NULL DEFAULT 6,
            cards_per_lesson INTEGER NOT NULL DEFAULT 12,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, subject_id),
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(subject_id) REFERENCES subjects(id)
        );
        """
    )

    # Notification preferences (per user)
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS user_prefs (
            user_id INTEGER PRIMARY KEY,
            email_enabled INTEGER NOT NULL DEFAULT 1,
            weekly_email_enabled INTEGER NOT NULL DEFAULT 1,
            weekly_email_day INTEGER NOT NULL DEFAULT 1, -- 0=Mon ... 6=Sun
            weekly_email_hour INTEGER NOT NULL DEFAULT 9, -- 0-23
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        """
    )

    conn.commit()
    conn.close()


# ---------- User management ----------

def create_user(username: str, password_hash: str, email: Optional[str] = None) -> bool:
    """
    Create a new user. Returns True on success, False if username already exists.
    """
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO users (username, password_hash, email)
            VALUES (?, ?, ?)
            """,
            (username, password_hash, email),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        # username already exists
        return False
    finally:
        conn.close()


def get_user_by_username(username: str) -> Optional[Tuple[int, str, str]]:
    """
    Returns (id, username, password_hash) or None.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, username, password_hash FROM users WHERE username = ?",
        (username,),
    )
    row = cursor.fetchone()
    conn.close()
    if row:
        return row[0], row[1], row[2]
    return None


def get_user_email(user_id: int) -> Optional[str]:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT email, username FROM users WHERE id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    email, username = row
    if isinstance(email, str) and email.strip():
        return email.strip()
    if isinstance(username, str) and re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", username.strip()):
        return username.strip()
    return None


def get_user_prefs(user_id: int) -> Dict:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO user_prefs (user_id) VALUES (?)", (user_id,))
    cur.execute(
        """
        SELECT email_enabled, weekly_email_enabled, weekly_email_day, weekly_email_hour
        FROM user_prefs
        WHERE user_id = ?
        """,
        (user_id,),
    )
    row = cur.fetchone()
    conn.commit()
    conn.close()
    email_enabled, weekly_enabled, weekly_day, weekly_hour = row if row else (1, 1, 1, 9)
    return {
        "email_enabled": bool(int(email_enabled)),
        "weekly_email_enabled": bool(int(weekly_enabled)),
        "weekly_email_day": int(weekly_day),
        "weekly_email_hour": int(weekly_hour),
    }


def update_user_prefs(
    *,
    user_id: int,
    email_enabled: bool,
    weekly_email_enabled: bool,
    weekly_email_day: int,
    weekly_email_hour: int,
) -> None:
    weekly_email_day = max(0, min(int(weekly_email_day), 6))
    weekly_email_hour = max(0, min(int(weekly_email_hour), 23))
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO user_prefs (user_id) VALUES (?)", (user_id,))
    cur.execute(
        """
        UPDATE user_prefs
        SET email_enabled = ?,
            weekly_email_enabled = ?,
            weekly_email_day = ?,
            weekly_email_hour = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE user_id = ?
        """,
        (
            1 if email_enabled else 0,
            1 if weekly_email_enabled else 0,
            weekly_email_day,
            weekly_email_hour,
            user_id,
        ),
    )
    conn.commit()
    conn.close()


def get_all_users() -> List[Tuple[int, str]]:
    """
    Returns list of (id, username) for all users.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, username FROM users ORDER BY username")
    rows = cursor.fetchall()
    conn.close()
    return rows


def update_user_username(user_id: int, new_username: str) -> bool:
    """
    Update a user's username. Returns True on success, False if username already exists.
    """
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "UPDATE users SET username = ? WHERE id = ?",
            (new_username, user_id),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def update_user_password(user_id: int, password_hash: str) -> None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (password_hash, user_id),
    )
    conn.commit()
    conn.close()


def delete_user(user_id: int) -> None:
    """
    Delete a user and all their dependent data (subjects, cards, attempts, uploads, logs).
    Does not delete physical files on disk (handled in app).
    """
    conn = get_connection()
    cursor = conn.cursor()

    # Get subject ids for this user
    cursor.execute("SELECT id FROM subjects WHERE user_id = ?", (user_id,))
    subj_rows = cursor.fetchall()
    subj_ids = [r[0] for r in subj_rows]

    card_ids: List[int] = []
    if subj_ids:
        placeholders = ",".join("?" * len(subj_ids))
        cursor.execute(
            f"SELECT id FROM cards WHERE subject_id IN ({placeholders})",
            subj_ids,
        )
        card_rows = cursor.fetchall()
        card_ids = [r[0] for r in card_rows]

    # Delete attempts by user_id
    cursor.execute("DELETE FROM card_attempts WHERE user_id = ?", (user_id,))

    # Delete attempts by cards (in case some are from other users, but tied to user's subjects)
    if card_ids:
        placeholders = ",".join("?" * len(card_ids))
        cursor.execute(
            f"DELETE FROM card_attempts WHERE card_id IN ({placeholders})",
            card_ids,
        )

    # Delete cards
    if card_ids:
        placeholders = ",".join("?" * len(card_ids))
        cursor.execute(
            f"DELETE FROM cards WHERE id IN ({placeholders})",
            card_ids,
        )

    # Delete uploaded_files
    cursor.execute("DELETE FROM uploaded_files WHERE user_id = ?", (user_id,))

    # Delete subjects
    cursor.execute("DELETE FROM subjects WHERE user_id = ?", (user_id,))

    # Delete admin logs
    cursor.execute(
        "DELETE FROM admin_logs WHERE admin_id = ? OR target_user_id = ?",
        (user_id, user_id),
    )

    # Finally delete user
    cursor.execute("DELETE FROM users WHERE id = ?", (user_id,))

    conn.commit()
    conn.close()


# ---------- Admin logs ----------

def admin_log(admin_id: int, target_user_id: int, action: str) -> None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO admin_logs (admin_id, target_user_id, action)
        VALUES (?, ?, ?)
        """,
        (admin_id, target_user_id, action),
    )
    conn.commit()
    conn.close()


def get_admin_logs(limit: int = 200) -> List[Dict]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT admin_id, target_user_id, action, timestamp
        FROM admin_logs
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    )
    rows = cursor.fetchall()
    conn.close()
    return [
        {
            "admin_id": r[0],
            "target_user_id": r[1],
            "action": r[2],
            "timestamp": r[3],
        }
        for r in rows
    ]


# ---------- SRS & card operations ----------

def _level_from_xp(xp: int) -> int:
    """
    Smooth leveling curve: level ~ sqrt(xp / 120) + 1
    - 0xp -> lvl 1
    - ~480xp -> lvl 3
    - ~1080xp -> lvl 4
    """
    if xp <= 0:
        return 1
    # avoid importing math for tiny function
    x = xp / 120.0
    # integer sqrt approximation via **0.5 is fine here
    lvl = int(x ** 0.5) + 1
    return max(1, lvl)


def _ensure_user_stats(cursor: sqlite3.Cursor, user_id: int) -> None:
    cursor.execute("INSERT OR IGNORE INTO user_stats (user_id) VALUES (?)", (user_id,))


def _ensure_subject_stats(cursor: sqlite3.Cursor, user_id: int, subject_id: int) -> None:
    cursor.execute(
        "INSERT OR IGNORE INTO subject_stats (user_id, subject_id) VALUES (?, ?)",
        (user_id, subject_id),
    )


def _award_xp_and_update_streak(
    *,
    conn: sqlite3.Connection,
    user_id: int,
    subject_id: int,
    is_correct: bool,
    quality: int,
    goal_attempts: int = 10,
) -> None:
    """
    Award XP for an attempt and manage streaks.
    Rewards both consistency (hitting daily goal) and accuracy/quality.
    """
    cursor = conn.cursor()
    today = date.today().isoformat()

    # XP rule
    base_xp = 5
    quality_bonus = max(0, min(int(quality), 5))  # 0..5
    correct_bonus = 5 if is_correct else 0
    xp_gain = base_xp + quality_bonus + correct_bonus

    _ensure_user_stats(cursor, user_id)
    _ensure_subject_stats(cursor, user_id, subject_id)

    cursor.execute(
        """
        INSERT INTO daily_progress (user_id, day, attempts, correct, xp, streak_counted)
        VALUES (?, ?, 0, 0, 0, 0)
        ON CONFLICT(user_id, day) DO NOTHING
        """,
        (user_id, today),
    )
    cursor.execute(
        """
        UPDATE daily_progress
        SET attempts = attempts + 1,
            correct = correct + ?,
            xp = xp + ?
        WHERE user_id = ? AND day = ?
        """,
        (1 if is_correct else 0, xp_gain, user_id, today),
    )

    # Update global XP + level
    cursor.execute("SELECT xp FROM user_stats WHERE user_id = ?", (user_id,))
    user_xp = int(cursor.fetchone()[0] or 0) + xp_gain
    cursor.execute("UPDATE user_stats SET xp = ?, level = ? WHERE user_id = ?", (user_xp, _level_from_xp(user_xp), user_id))

    # Update per-subject XP + level
    cursor.execute("SELECT xp FROM subject_stats WHERE user_id = ? AND subject_id = ?", (user_id, subject_id))
    subj_xp = int(cursor.fetchone()[0] or 0) + xp_gain
    cursor.execute(
        "UPDATE subject_stats SET xp = ?, level = ? WHERE user_id = ? AND subject_id = ?",
        (subj_xp, _level_from_xp(subj_xp), user_id, subject_id),
    )

    # Streak: only increments when daily attempts reaches the goal and hasn't been counted
    cursor.execute(
        "SELECT attempts, streak_counted FROM daily_progress WHERE user_id = ? AND day = ?",
        (user_id, today),
    )
    attempts_today, streak_counted = cursor.fetchone()
    attempts_today = int(attempts_today or 0)
    streak_counted = int(streak_counted or 0)

    if attempts_today >= goal_attempts and streak_counted == 0:
        # Determine whether streak continues
        cursor.execute("SELECT current_streak, best_streak, last_streak_date FROM user_stats WHERE user_id = ?", (user_id,))
        current_streak, best_streak, last_streak_date = cursor.fetchone()
        current_streak = int(current_streak or 0)
        best_streak = int(best_streak or 0)

        yesterday = (date.today() - timedelta(days=1)).isoformat()
        if last_streak_date == yesterday:
            current_streak += 1
        elif last_streak_date == today:
            # already counted today (shouldn't happen because streak_counted=0),
            # but keep safe
            current_streak = max(current_streak, 1)
        else:
            current_streak = 1

        best_streak = max(best_streak, current_streak)

        cursor.execute(
            "UPDATE user_stats SET current_streak=?, best_streak=?, last_streak_date=? WHERE user_id=?",
            (current_streak, best_streak, today, user_id),
        )
        cursor.execute(
            "UPDATE daily_progress SET streak_counted = 1 WHERE user_id = ? AND day = ?",
            (user_id, today),
        )


# ---------- Achievements & mastery ----------

_ACH_USER_FIRST_10 = "first_10_attempts"
_ACH_USER_STREAK_3 = "streak_3"
_ACH_USER_STREAK_7 = "streak_7"
_ACH_USER_STREAK_30 = "streak_30"
_ACH_USER_DAY_90_ACC = "day_accuracy_90_20"

_ACH_SUBJ_FIRST_50_XP = "subject_50_xp"
_ACH_SUBJ_MASTERY_60 = "subject_mastery_60"
_ACH_SUBJ_MASTERY_85 = "subject_mastery_85"


def _get_subject_mastery(cur: sqlite3.Cursor, user_id: int, subject_id: int) -> Dict[str, int]:
    """
    Mastery proxy based on SRS repetitions:
    - mastered if repetitions >= 2
    """
    cur.execute(
        """
        SELECT COUNT(*),
               SUM(CASE WHEN c.repetitions >= 2 THEN 1 ELSE 0 END)
        FROM cards c
        JOIN subjects s ON c.subject_id = s.id
        WHERE c.subject_id = ? AND s.user_id = ?
        """,
        (subject_id, user_id),
    )
    total, mastered = cur.fetchone()
    total = int(total or 0)
    mastered = int(mastered or 0)
    pct = int(round((mastered / total) * 100)) if total else 0
    return {"total": total, "mastered": mastered, "pct": pct}


def _award_achievements(conn: sqlite3.Connection, user_id: int, subject_id: int) -> Dict[str, List[str]]:
    """
    Award new achievements and return dict of newly earned codes:
    { "user": [...], "subject": [...] }
    """
    cur = conn.cursor()
    today = date.today().isoformat()
    new_user: List[str] = []
    new_subject: List[str] = []

    # Total attempts (user)
    cur.execute("SELECT COUNT(*) FROM card_attempts WHERE user_id = ?", (user_id,))
    total_attempts = int(cur.fetchone()[0] or 0)
    if total_attempts >= 10:
        cur.execute("INSERT OR IGNORE INTO user_achievements (user_id, code) VALUES (?, ?)", (user_id, _ACH_USER_FIRST_10))
        if cur.rowcount:
            new_user.append(_ACH_USER_FIRST_10)

    # Streak-based
    cur.execute("SELECT current_streak FROM user_stats WHERE user_id = ?", (user_id,))
    streak = int((cur.fetchone() or [0])[0] or 0)
    for threshold, code in [(3, _ACH_USER_STREAK_3), (7, _ACH_USER_STREAK_7), (30, _ACH_USER_STREAK_30)]:
        if streak >= threshold:
            cur.execute("INSERT OR IGNORE INTO user_achievements (user_id, code) VALUES (?, ?)", (user_id, code))
            if cur.rowcount:
                new_user.append(code)

    # High-accuracy day: >=20 attempts today and >=90% correct
    cur.execute("SELECT attempts, correct FROM daily_progress WHERE user_id = ? AND day = ?", (user_id, today))
    row = cur.fetchone()
    if row:
        attempts_today, correct_today = int(row[0] or 0), int(row[1] or 0)
        if attempts_today >= 20 and attempts_today > 0 and (correct_today / attempts_today) >= 0.90:
            cur.execute("INSERT OR IGNORE INTO user_achievements (user_id, code) VALUES (?, ?)", (user_id, _ACH_USER_DAY_90_ACC))
            if cur.rowcount:
                new_user.append(_ACH_USER_DAY_90_ACC)

    # Subject XP thresholds
    _ensure_subject_stats(cur, user_id, subject_id)
    cur.execute("SELECT xp FROM subject_stats WHERE user_id = ? AND subject_id = ?", (user_id, subject_id))
    subj_xp = int(cur.fetchone()[0] or 0)
    if subj_xp >= 50:
        cur.execute(
            "INSERT OR IGNORE INTO subject_achievements (user_id, subject_id, code) VALUES (?, ?, ?)",
            (user_id, subject_id, _ACH_SUBJ_FIRST_50_XP),
        )
        if cur.rowcount:
            new_subject.append(_ACH_SUBJ_FIRST_50_XP)

    mastery = _get_subject_mastery(cur, user_id, subject_id)
    if mastery["pct"] >= 60:
        cur.execute(
            "INSERT OR IGNORE INTO subject_achievements (user_id, subject_id, code) VALUES (?, ?, ?)",
            (user_id, subject_id, _ACH_SUBJ_MASTERY_60),
        )
        if cur.rowcount:
            new_subject.append(_ACH_SUBJ_MASTERY_60)
    if mastery["pct"] >= 85:
        cur.execute(
            "INSERT OR IGNORE INTO subject_achievements (user_id, subject_id, code) VALUES (?, ?, ?)",
            (user_id, subject_id, _ACH_SUBJ_MASTERY_85),
        )
        if cur.rowcount:
            new_subject.append(_ACH_SUBJ_MASTERY_85)

    return {"user": new_user, "subject": new_subject}


def record_attempt(card_id: int, subject_id: int, user_id: int, is_correct: bool, quality: int) -> Dict[str, List[str]]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO card_attempts (card_id, subject_id, user_id, is_correct, quality)
        VALUES (?, ?, ?, ?, ?)
        """,
        (card_id, subject_id, user_id, 1 if is_correct else 0, quality),
    )
    _award_xp_and_update_streak(
        conn=conn,
        user_id=user_id,
        subject_id=subject_id,
        is_correct=is_correct,
        quality=quality,
        goal_attempts=10,
    )
    newly_earned = _award_achievements(conn, user_id, subject_id)
    conn.commit()
    conn.close()
    return newly_earned


def get_gamification_summary(user_id: int, subject_id: Optional[int] = None) -> Dict:
    """
    Returns gamification snapshot for header/UI.
    """
    conn = get_connection()
    cur = conn.cursor()
    today = date.today().isoformat()

    cur.execute("INSERT OR IGNORE INTO user_stats (user_id) VALUES (?)", (user_id,))
    cur.execute("SELECT xp, level, current_streak, best_streak, last_streak_date FROM user_stats WHERE user_id = ?", (user_id,))
    xp, level, current_streak, best_streak, last_streak_date = cur.fetchone()

    cur.execute(
        """
        SELECT attempts, correct, xp, streak_counted
        FROM daily_progress
        WHERE user_id = ? AND day = ?
        """,
        (user_id, today),
    )
    row = cur.fetchone()
    if row:
        attempts_today, correct_today, xp_today, streak_counted = row
    else:
        attempts_today, correct_today, xp_today, streak_counted = (0, 0, 0, 0)

    out: Dict = {
        "goal_attempts": 10,
        "attempts_today": int(attempts_today or 0),
        "correct_today": int(correct_today or 0),
        "xp_today": int(xp_today or 0),
        "xp": int(xp or 0),
        "level": int(level or 1),
        "streak": int(current_streak or 0),
        "best_streak": int(best_streak or 0),
        "last_streak_date": last_streak_date,
        "streak_done_today": bool(streak_counted),
    }

    if subject_id is not None:
        cur.execute(
            "INSERT OR IGNORE INTO subject_stats (user_id, subject_id) VALUES (?, ?)",
            (user_id, subject_id),
        )
        cur.execute("SELECT xp, level FROM subject_stats WHERE user_id = ? AND subject_id = ?", (user_id, subject_id))
        sxp, slevel = cur.fetchone()
        mastery = _get_subject_mastery(cur, user_id, subject_id)
        out["subject"] = {
            "subject_id": subject_id,
            "xp": int(sxp or 0),
            "level": int(slevel or 1),
            "mastery_pct": mastery["pct"],
            "mastered_cards": mastery["mastered"],
            "total_cards": mastery["total"],
        }

    conn.commit()
    conn.close()
    return out


def get_earned_achievements(user_id: int, subject_id: Optional[int] = None) -> Dict[str, List[str]]:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT code FROM user_achievements WHERE user_id = ? ORDER BY earned_at", (user_id,))
    user_codes = [r[0] for r in cur.fetchall()]
    subj_codes: List[str] = []
    if subject_id is not None:
        cur.execute(
            "SELECT code FROM subject_achievements WHERE user_id = ? AND subject_id = ? ORDER BY earned_at",
            (user_id, subject_id),
        )
        subj_codes = [r[0] for r in cur.fetchall()]
    conn.close()
    return {"user": user_codes, "subject": subj_codes}


def update_card_schedule(card_id: int, quality: int):
    """
    Update card's schedule using SM-2.
    quality: 0-5
    """
    conn = get_connection()
    cursor = conn.cursor()

    # Load current SRS fields
    cursor.execute(
        """
        SELECT ef, interval, repetitions
        FROM cards
        WHERE id = ?
        """,
        (card_id,),
    )
    row = cursor.fetchone()
    if row is None:
        conn.close()
        return

    ef, interval, repetitions = row
    ef = float(ef)
    interval = int(interval)
    repetitions = int(repetitions)

    # SM-2 algorithm
    if quality < 3:
        repetitions = 0
        interval = 1
        lapse_increment = 1
    else:
        lapse_increment = 0
        if repetitions == 0:
            interval = 1
        elif repetitions == 1:
            interval = 6
        else:
            interval = int(round(interval * ef))
        repetitions += 1

    # Update EF (easiness factor)
    ef = ef + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
    if ef < 1.3:
        ef = 1.3

    today = date.today()
    due = today + timedelta(days=interval)
    now_str = datetime.now().isoformat(timespec="seconds")
    due_str = due.isoformat()

    cursor.execute(
        """
        UPDATE cards
        SET ef = ?, interval = ?, repetitions = ?,
            due_date = ?, last_review = ?, lapse_count = lapse_count + ?
        WHERE id = ?
        """,
        (ef, interval, repetitions, due_str, now_str, lapse_increment, card_id),
    )

    conn.commit()
    conn.close()


def get_due_cards(
    subject_id: int,
    limit: int = 100,
    *,
    user_id: Optional[int] = None,
    lesson_id: Optional[int] = None,
) -> List[QAItem]:
    conn = get_connection()
    cursor = conn.cursor()

    if user_id is None and lesson_id is None:
        cursor.execute(
            """
            SELECT id, card_type, question, answer, source_pdf, page, subject_id, options
            FROM cards
            WHERE subject_id = ?
              AND DATE(due_date) <= DATE('now')
            ORDER BY due_date ASC, id ASC
            LIMIT ?
            """,
            (subject_id, limit),
        )
    else:
        # When user_id is provided, filter out gated track cards (unless unlocked).
        # When lesson_id is provided, additionally restrict to that lesson.
        cursor.execute(
            """
            SELECT c.id, c.card_type, c.question, c.answer, c.source_pdf, c.page, c.subject_id, c.options
            FROM cards c
            JOIN subjects s ON c.subject_id = s.id
            LEFT JOIN tracks t ON c.track_id = t.id
            LEFT JOIN lessons l ON c.lesson_id = l.id
            WHERE s.user_id = ?
              AND c.subject_id = ?
              AND DATE(c.due_date) <= DATE('now')
              AND (? IS NULL OR c.lesson_id = ?)
              AND (
                c.track_id IS NULL
                OR t.open_all = 1
                OR l.lesson_index <= t.unlocked_lesson_index
              )
            ORDER BY c.due_date ASC, c.id ASC
            LIMIT ?
            """,
            (user_id, subject_id, lesson_id, lesson_id, limit),
        )
    rows = cursor.fetchall()
    conn.close()

    cards: List[QAItem] = []
    for row in rows:
        options_json = row[7]
        options = json.loads(options_json) if options_json else None
        cards.append(
            QAItem(
                id=row[0],
                card_type=row[1],
                question=row[2],
                answer=row[3],
                source_pdf=row[4],
                page=row[5],
                subject_id=row[6],
                options=options,
            )
        )
    return cards


def get_subject_stats(subject_id: int, user_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT COUNT(*), SUM(is_correct)
        FROM card_attempts
        WHERE subject_id = ? AND user_id = ?
        """,
        (subject_id, user_id),
    )
    total, correct = cursor.fetchone()
    conn.close()
    return total or 0, correct or 0


def get_card_stats(card_id: int, user_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT COUNT(*), SUM(is_correct)
        FROM card_attempts
        WHERE card_id = ? AND user_id = ?
        """,
        (card_id, user_id),
    )
    total, correct = cursor.fetchone()
    conn.close()
    return total or 0, correct or 0


def insert_card(card: QAItem, *, track_id: Optional[int] = None, lesson_id: Optional[int] = None):
    conn = get_connection()
    cursor = conn.cursor()
    options_json = json.dumps(card.options) if card.options is not None else None
    cursor.execute(
        """
        INSERT INTO cards (
            card_type, question, answer, source_pdf, page, subject_id,
            track_id, lesson_id,
            options, ef, interval, repetitions, due_date, last_review, lapse_count
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            card.card_type,
            card.question,
            card.answer,
            card.source_pdf,
            card.page,
            card.subject_id,
            track_id,
            lesson_id,
            options_json,
            2.5,  # ef default
            0,    # interval
            0,    # repetitions
            date.today().isoformat(),  # due today initially
            None,  # last_review
            0,     # lapse_count
        ),
    )
    conn.commit()
    conn.close()


def load_all_cards(user_id: Optional[int] = None) -> List[QAItem]:
    """
    If user_id is provided, load only cards whose subject belongs to that user.
    Otherwise load all cards.
    """
    conn = get_connection()
    cursor = conn.cursor()

    if user_id is None:
        cursor.execute(
            """
            SELECT id, card_type, question, answer, source_pdf, page, subject_id, options
            FROM cards
            """
        )
        rows = cursor.fetchall()
    else:
        cursor.execute(
            """
            SELECT c.id, c.card_type, c.question, c.answer,
                   c.source_pdf, c.page, c.subject_id, c.options
            FROM cards c
            JOIN subjects s ON c.subject_id = s.id
            WHERE s.user_id = ?
            """,
            (user_id,),
        )
        rows = cursor.fetchall()

    conn.close()

    cards: List[QAItem] = []
    for row in rows:
        options_json = row[7]
        options = json.loads(options_json) if options_json else None
        cards.append(
            QAItem(
                id=row[0],
                card_type=row[1],
                question=row[2],
                answer=row[3],
                source_pdf=row[4],
                page=row[5],
                subject_id=row[6],
                options=options,
            )
        )
    return cards


# ---------- Tracks & lesson gating ----------

def create_track(
    *,
    user_id: int,
    subject_id: int,
    title: str,
    num_lessons: int,
    cards_per_lesson: int,
) -> int:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO tracks (user_id, subject_id, title, num_lessons, cards_per_lesson, unlocked_lesson_index, open_all)
        VALUES (?, ?, ?, ?, ?, 0, 0)
        """,
        (user_id, subject_id, title, int(num_lessons), int(cards_per_lesson)),
    )
    track_id = int(cursor.lastrowid)
    conn.commit()
    conn.close()
    return track_id


def insert_lesson(
    *,
    track_id: int,
    lesson_index: int,
    title: str,
    objectives: list[str],
    anchor_queries: list[str],
    brief: Optional[str] = None,
    bite_markdown: Optional[str] = None,
) -> int:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO lessons (track_id, lesson_index, title, objectives, anchor_queries, brief, bite_markdown)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (track_id, int(lesson_index), title, json.dumps(objectives), json.dumps(anchor_queries), brief, bite_markdown),
    )
    lesson_id = int(cursor.lastrowid)
    conn.commit()
    conn.close()
    return lesson_id


def get_tracks(user_id: int) -> List[dict]:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT t.id, t.title, t.subject_id, s.name, t.num_lessons, t.cards_per_lesson, t.unlocked_lesson_index, t.open_all, t.created_at
        FROM tracks t
        JOIN subjects s ON t.subject_id = s.id
        WHERE t.user_id = ?
        ORDER BY t.created_at DESC, t.id DESC
        """,
        (user_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "id": r[0],
            "title": r[1],
            "subject_id": r[2],
            "subject_name": r[3],
            "num_lessons": r[4],
            "cards_per_lesson": r[5],
            "unlocked_lesson_index": r[6],
            "open_all": bool(r[7]),
            "created_at": r[8],
        }
        for r in rows
    ]


def get_track(user_id: int, track_id: int) -> Optional[dict]:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT t.id, t.title, t.subject_id, s.name, t.num_lessons, t.cards_per_lesson, t.unlocked_lesson_index, t.open_all, t.created_at
        FROM tracks t
        JOIN subjects s ON t.subject_id = s.id
        WHERE t.user_id = ? AND t.id = ?
        """,
        (user_id, track_id),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row[0],
        "title": row[1],
        "subject_id": row[2],
        "subject_name": row[3],
        "num_lessons": int(row[4]),
        "cards_per_lesson": int(row[5]),
        "unlocked_lesson_index": int(row[6]),
        "open_all": bool(row[7]),
        "created_at": row[8],
    }


def get_lessons(user_id: int, track_id: int) -> List[dict]:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT l.id, l.lesson_index, l.title, l.objectives, l.anchor_queries, l.brief, l.bite_markdown, t.unlocked_lesson_index, t.open_all
        FROM lessons l
        JOIN tracks t ON l.track_id = t.id
        WHERE t.user_id = ? AND t.id = ?
        ORDER BY l.lesson_index ASC
        """,
        (user_id, track_id),
    )
    rows = cur.fetchall()
    conn.close()
    if not rows:
        return []
    unlocked = int(rows[0][7] or 0)
    open_all = bool(rows[0][8])
    out = []
    for r in rows:
        lesson_id, lesson_index, title, objectives_json, anchor_json, brief, bite_markdown = r[0:7]
        try:
            objectives = json.loads(objectives_json) if objectives_json else []
        except json.JSONDecodeError:
            objectives = []
        try:
            anchor_queries = json.loads(anchor_json) if anchor_json else []
        except json.JSONDecodeError:
            anchor_queries = []
        is_unlocked = open_all or int(lesson_index) <= unlocked
        out.append(
            {
                "id": int(lesson_id),
                "lesson_index": int(lesson_index),
                "title": str(title),
                "objectives": objectives,
                "anchor_queries": anchor_queries,
                "brief": brief,
                "bite_markdown": bite_markdown,
                "is_unlocked": is_unlocked,
            }
        )
    return out


def get_track_generation_defaults(user_id: int, subject_id: int) -> Dict[str, int]:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT num_lessons, cards_per_lesson
        FROM track_generation_defaults
        WHERE user_id = ? AND subject_id = ?
        """,
        (user_id, subject_id),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return {"num_lessons": 6, "cards_per_lesson": 12}
    return {"num_lessons": int(row[0] or 6), "cards_per_lesson": int(row[1] or 12)}


def set_track_generation_defaults(user_id: int, subject_id: int, *, num_lessons: int, cards_per_lesson: int) -> None:
    num_lessons = max(6, min(int(num_lessons), 10))
    cards_per_lesson = max(1, min(int(cards_per_lesson), 50))
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO track_generation_defaults (user_id, subject_id, num_lessons, cards_per_lesson)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id, subject_id) DO UPDATE SET
            num_lessons=excluded.num_lessons,
            cards_per_lesson=excluded.cards_per_lesson,
            updated_at=CURRENT_TIMESTAMP
        """,
        (user_id, subject_id, num_lessons, cards_per_lesson),
    )
    conn.commit()
    conn.close()


def count_user_tracks(user_id: int, subject_id: int) -> int:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM tracks WHERE user_id = ? AND subject_id = ?",
        (user_id, subject_id),
    )
    row = cur.fetchone()
    conn.close()
    return int(row[0] or 0)


def delete_track_for_user(*, user_id: int, track_id: int) -> bool:
    """
    Deletes:
      - lessons under the track
      - cards tagged with the track
      - card_attempts for those cards
      - the track row
    Returns True if deleted, False if not found / not owned.
    """
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT id FROM tracks WHERE id = ? AND user_id = ?", (track_id, user_id))
    if not cur.fetchone():
        conn.close()
        return False

    # Cards for this track (used to delete attempts first).
    cur.execute("SELECT id FROM cards WHERE track_id = ?", (track_id,))
    card_rows = cur.fetchall()
    card_ids = [r[0] for r in card_rows]

    # Delete attempts for the cards (if any)
    if card_ids:
        placeholders = ",".join("?" * len(card_ids))
        cur.execute(f"DELETE FROM card_attempts WHERE card_id IN ({placeholders})", card_ids)

    # Delete cards
    cur.execute("DELETE FROM cards WHERE track_id = ?", (track_id,))

    # Delete lessons
    cur.execute("DELETE FROM lessons WHERE track_id = ?", (track_id,))

    # Delete track
    cur.execute("DELETE FROM tracks WHERE id = ? AND user_id = ?", (track_id, user_id))

    conn.commit()
    conn.close()
    return True


def set_open_all_for_all_tracks(*, user_id: Optional[int] = None, subject_id: Optional[int] = None) -> int:
    """
    Bulk admin operation:
    - Sets `tracks.open_all=1` for matching tracks.
    - Sets `unlocked_lesson_index` to `num_lessons-1`.
    - Returns count of affected rows.

    Optional filters:
      - user_id: only tracks belonging to this user
      - subject_id: only tracks for this subject
    If neither is provided, applies to all tracks.
    """
    conn = get_connection()
    cur = conn.cursor()

    where = []
    params: list[object] = []
    if user_id is not None:
        where.append("user_id = ?")
        params.append(int(user_id))
    if subject_id is not None:
        where.append("subject_id = ?")
        params.append(int(subject_id))

    where_sql = ""
    if where:
        where_sql = "WHERE " + " AND ".join(where)

    cur.execute(
        f"""
        UPDATE tracks
        SET open_all = 1,
            unlocked_lesson_index = CASE
                WHEN num_lessons > 0 THEN (num_lessons - 1)
                ELSE 0
            END
        {where_sql}
        """,
        params,
    )
    changed = int(cur.rowcount or 0)
    conn.commit()
    conn.close()
    return changed


def gate_all_lessons_for_all_users(*, user_id: Optional[int] = None, subject_id: Optional[int] = None) -> int:
    """
    Bulk admin operation:
    - Sets `tracks.open_all=0` (remove admin override).
    - Recomputes `tracks.unlocked_lesson_index` for each track based on current card mastery.

    A lesson is considered "passed" when:
      mastered_cards >= threshold(total_cards)
    where threshold(total_cards) matches the same logic used by unlocking.
    Unlocking is sequential: lesson N unlocks only if all lessons < N are passed.

    Returns count of tracks updated.
    """
    conn = get_connection()
    cur = conn.cursor()

    where = []
    params: list[object] = []
    if user_id is not None:
        where.append("t.user_id = ?")
        params.append(int(user_id))
    if subject_id is not None:
        where.append("t.subject_id = ?")
        params.append(int(subject_id))

    where_sql = ""
    if where:
        where_sql = "WHERE " + " AND ".join(where)

    cur.execute(
        f"""
        SELECT t.id, t.num_lessons
        FROM tracks t
        {where_sql}
        """,
        params,
    )
    track_rows = cur.fetchall()

    updated = 0
    for track_id, num_lessons in track_rows:
        num_lessons = int(num_lessons or 0)
        if num_lessons <= 0:
            # Keep as locked.
            cur.execute(
                "UPDATE tracks SET open_all=0, unlocked_lesson_index=0 WHERE id=?",
                (int(track_id),),
            )
            updated += 1
            continue

        unlocked_idx = 0
        # sequentially check passed lessons
        for lesson_idx in range(num_lessons):
            cur.execute(
                "SELECT id FROM lessons WHERE track_id = ? AND lesson_index = ?",
                (int(track_id), lesson_idx),
            )
            lesson_row = cur.fetchone()
            if not lesson_row:
                break
            lesson_id = int(lesson_row[0])

            cur.execute("SELECT COUNT(*) FROM cards WHERE track_id=? AND lesson_id=?", (int(track_id), lesson_id))
            total_cards = int(cur.fetchone()[0] or 0)
            if total_cards <= 0:
                break

            cur.execute(
                "SELECT COUNT(*) FROM cards WHERE track_id=? AND lesson_id=? AND repetitions >= 2",
                (int(track_id), lesson_id),
            )
            mastered_cards = int(cur.fetchone()[0] or 0)

            threshold = _get_track_mastery_threshold(total_cards)
            if mastered_cards < threshold:
                break

            unlocked_idx = lesson_idx

        cur.execute(
            "UPDATE tracks SET open_all=0, unlocked_lesson_index=? WHERE id=?",
            (unlocked_idx, int(track_id)),
        )
        updated += 1

    conn.commit()
    conn.close()
    return updated


def set_track_open_all(user_id: int, track_id: int, *, open_all: bool) -> bool:
    conn = get_connection()
    cur = conn.cursor()
    if open_all:
        cur.execute(
            """
            UPDATE tracks
            SET open_all=1,
                unlocked_lesson_index = CASE WHEN num_lessons > 0 THEN (num_lessons - 1) ELSE 0 END
            WHERE id=? AND user_id=?
            """,
            (track_id, user_id),
        )
    else:
        cur.execute(
            """
            UPDATE tracks
            SET open_all=0
            WHERE id=? AND user_id=?
            """,
            (track_id, user_id),
        )
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return bool(changed)


def _get_track_mastery_threshold(total_cards: int) -> int:
    # Unlock when a lesson is "mostly mastered".
    # With 12 cards/lesson, this yields: max(3, int(12*0.6)=7) => 7 mastered cards.
    if total_cards <= 0:
        return 0
    return min(total_cards, max(3, int(total_cards * 0.6)))


def maybe_unlock_next_lesson(*, user_id: int, track_id: int) -> int:
    """
    If the currently-unlocked lesson is sufficiently mastered, unlock the next one.
    Returns the (possibly updated) unlocked_lesson_index.
    """
    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT unlocked_lesson_index, open_all, num_lessons
        FROM tracks
        WHERE user_id = ? AND id = ?
        """,
        (user_id, track_id),
    )
    row = cur.fetchone()
    if not row:
        conn.close()
        return 0
    unlocked_idx = int(row[0] or 0)
    open_all = bool(row[1])
    num_lessons = int(row[2] or 0)

    if open_all or unlocked_idx >= max(0, num_lessons - 1):
        conn.close()
        return unlocked_idx

    # Unlock multiple lessons if the user is already far ahead.
    while unlocked_idx < max(0, num_lessons - 1):
        cur.execute(
            "SELECT id FROM lessons WHERE track_id = ? AND lesson_index = ?",
            (track_id, unlocked_idx),
        )
        lesson_row = cur.fetchone()
        if not lesson_row:
            break
        lesson_id = int(lesson_row[0])

        cur.execute(
            "SELECT COUNT(*) FROM cards WHERE track_id = ? AND lesson_id = ?",
            (track_id, lesson_id),
        )
        total_cards = int(cur.fetchone()[0] or 0)
        if total_cards <= 0:
            break

        cur.execute(
            "SELECT COUNT(*) FROM cards WHERE track_id = ? AND lesson_id = ? AND repetitions >= 2",
            (track_id, lesson_id),
        )
        mastered_cards = int(cur.fetchone()[0] or 0)
        threshold = _get_track_mastery_threshold(total_cards)

        if mastered_cards < threshold:
            break

        unlocked_idx += 1

    if unlocked_idx != int(row[0] or 0):
        cur.execute(
            "UPDATE tracks SET unlocked_lesson_index = ? WHERE user_id = ? AND id = ?",
            (unlocked_idx, user_id, track_id),
        )
    conn.commit()
    conn.close()
    return unlocked_idx


def get_card_track_lesson(card_id: int, user_id: int) -> Optional[tuple[int, int]]:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT c.track_id, c.lesson_id
        FROM cards c
        JOIN subjects s ON c.subject_id = s.id
        WHERE c.id = ? AND s.user_id = ?
        """,
        (card_id, user_id),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    track_id, lesson_id = row
    if track_id is None or lesson_id is None:
        return None
    return int(track_id), int(lesson_id)


def add_subject(name: str, user_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR IGNORE INTO subjects (name, user_id) VALUES (?, ?)",
        (name, user_id),
    )
    conn.commit()
    conn.close()


def get_subjects(user_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, name FROM subjects WHERE user_id = ? ORDER BY name",
        (user_id,),
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_subject_id(name: str, user_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id FROM subjects WHERE name = ? AND user_id = ?",
        (name, user_id),
    )
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


# ---------- Uploaded files management ----------

def insert_uploaded_file(user_id: int, subject_id: int, filename: str, stored_path: str) -> int:
    """
    Store metadata for an uploaded PDF file.
    Returns the new uploaded_files.id.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO uploaded_files (user_id, subject_id, filename, stored_path)
        VALUES (?, ?, ?, ?)
        """,
        (user_id, subject_id, filename, stored_path),
    )
    conn.commit()
    file_id = cursor.lastrowid
    conn.close()
    return file_id


def get_uploaded_files(user_id: int, subject_id: int):
    """
    Return a list of dicts: [{id, filename, stored_path, uploaded_at}, ...]
    for this user & subject.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, filename, stored_path, uploaded_at
        FROM uploaded_files
        WHERE user_id = ? AND subject_id = ?
        ORDER BY uploaded_at DESC
        """,
        (user_id, subject_id),
    )
    rows = cursor.fetchall()
    conn.close()
    return [
        {
            "id": r[0],
            "filename": r[1],
            "stored_path": r[2],
            "uploaded_at": r[3],
        }
        for r in rows
    ]


def delete_uploaded_file_and_cards(uploaded_file_id: int, user_id: int) -> Optional[str]:
    """
    Delete a file metadata row and ALL cards + attempts referencing that file
    for that user. Returns stored_path so the app can delete the physical file.
    """
    conn = get_connection()
    cursor = conn.cursor()

    # Lookup file row and validate user ownership
    cursor.execute(
        """
        SELECT id, filename, stored_path, subject_id
        FROM uploaded_files
        WHERE id = ? AND user_id = ?
        """,
        (uploaded_file_id, user_id),
    )
    row = cursor.fetchone()
    if row is None:
        conn.close()
        return None

    _, filename, stored_path, subject_id = row

    # Find cards belonging to that subject + source_pdf = filename
    cursor.execute(
        """
        SELECT c.id
        FROM cards c
        JOIN subjects s ON c.subject_id = s.id
        WHERE c.subject_id = ?
          AND c.source_pdf = ?
          AND s.user_id = ?
        """,
        (subject_id, filename, user_id),
    )
    card_rows = cursor.fetchall()
    card_ids = [r[0] for r in card_rows]

    # Delete attempts & cards
    if card_ids:
        placeholders = ",".join("?" * len(card_ids))
        cursor.execute(
            f"DELETE FROM card_attempts WHERE card_id IN ({placeholders})",
            card_ids,
        )
        cursor.execute(
            f"DELETE FROM cards WHERE id IN ({placeholders})",
            card_ids,
        )

    # Delete uploaded_files row
    cursor.execute(
        "DELETE FROM uploaded_files WHERE id = ? AND user_id = ?",
        (uploaded_file_id, user_id),
    )

    conn.commit()
    conn.close()
    return stored_path


def delete_card(card_id: int, user_id: int) -> bool:
    """
    Delete a single card (and its attempts) if it belongs to this user.
    Returns True if deleted, False if not found or not owned.
    """
    conn = get_connection()
    cursor = conn.cursor()

    # Verify ownership via subject.user_id
    cursor.execute(
        """
        SELECT c.id
        FROM cards c
        JOIN subjects s ON c.subject_id = s.id
        WHERE c.id = ? AND s.user_id = ?
        """,
        (card_id, user_id),
    )
    row = cursor.fetchone()
    if row is None:
        conn.close()
        return False

    # Delete attempts then card
    cursor.execute("DELETE FROM card_attempts WHERE card_id = ?", (card_id,))
    cursor.execute("DELETE FROM cards WHERE id = ?", (card_id,))

    conn.commit()
    conn.close()
    return True

def update_excluded_pages(file_id: int, excluded: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE uploaded_files SET excluded_pages=? WHERE id=?",
        (excluded, file_id)
    )
    conn.commit()
    conn.close()


def get_excluded_pages_map(file_id: int) -> list[int]:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT excluded_pages FROM uploaded_files WHERE id=?", (file_id,))
    row = cur.fetchone()
    conn.close()

    if not row or not row[0]:
        return []

    # parse formats like "1,2,5-8"
    text = row[0]
    pages = []
    for part in text.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-")
            pages.extend(range(int(a), int(b) + 1))
        else:
            pages.append(int(part))
    return pages