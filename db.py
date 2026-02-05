# db.py
import os
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
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        """
    )

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

    conn.commit()
    conn.close()


# ---------- User management ----------

def create_user(username: str, password_hash: str) -> bool:
    """
    Create a new user. Returns True on success, False if username already exists.
    """
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO users (username, password_hash)
            VALUES (?, ?)
            """,
            (username, password_hash),
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

def record_attempt(card_id: int, subject_id: int, user_id: int, is_correct: bool, quality: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO card_attempts (card_id, subject_id, user_id, is_correct, quality)
        VALUES (?, ?, ?, ?, ?)
        """,
        (card_id, subject_id, user_id, 1 if is_correct else 0, quality),
    )
    conn.commit()
    conn.close()


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


def get_due_cards(subject_id: int, limit: int = 100) -> List[QAItem]:
    conn = get_connection()
    cursor = conn.cursor()
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


def insert_card(card: QAItem):
    conn = get_connection()
    cursor = conn.cursor()
    options_json = json.dumps(card.options) if card.options is not None else None
    cursor.execute(
        """
        INSERT INTO cards (
            card_type, question, answer, source_pdf, page, subject_id,
            options, ef, interval, repetitions, due_date, last_review, lapse_count
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            card.card_type,
            card.question,
            card.answer,
            card.source_pdf,
            card.page,
            card.subject_id,
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