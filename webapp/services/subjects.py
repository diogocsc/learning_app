from __future__ import annotations

import os
from typing import Optional

from config import UPLOAD_DIR
from db import get_connection


def delete_subject_for_user(*, subject_id: int, user_id: int) -> bool:
    """
    Delete a subject and all associated data for a user:
    - cards
    - card_attempts (for those cards)
    - uploaded_files rows
    - physical uploaded PDF files

    Returns True if deleted, False if subject not found / not owned.
    """
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT id FROM subjects WHERE id = ? AND user_id = ?", (subject_id, user_id))
    row = cur.fetchone()
    if not row:
        conn.close()
        return False

    # Cards for this subject
    cur.execute("SELECT id FROM cards WHERE subject_id = ?", (subject_id,))
    card_ids = [r[0] for r in cur.fetchall()]

    if card_ids:
        placeholders = ",".join("?" * len(card_ids))
        # delete attempts for these cards (all users) to keep DB consistent
        cur.execute(f"DELETE FROM card_attempts WHERE card_id IN ({placeholders})", card_ids)
        cur.execute(f"DELETE FROM cards WHERE id IN ({placeholders})", card_ids)

    # Physical files on disk (best-effort)
    cur.execute(
        "SELECT stored_path FROM uploaded_files WHERE user_id = ? AND subject_id = ?",
        (user_id, subject_id),
    )
    for (stored_path,) in cur.fetchall():
        if stored_path and os.path.exists(stored_path):
            try:
                os.remove(stored_path)
            except OSError:
                pass

    # Metadata rows
    cur.execute("DELETE FROM uploaded_files WHERE user_id = ? AND subject_id = ?", (user_id, subject_id))
    cur.execute("DELETE FROM subjects WHERE id = ? AND user_id = ?", (subject_id, user_id))

    conn.commit()
    conn.close()

    # Also try to remove the subject upload directory if empty
    subj_dir = UPLOAD_DIR / f"user_{user_id}" / f"subject_{subject_id}"
    if subj_dir.exists():
        try:
            # remove empty dirs bottom-up
            for root, dirs, files in os.walk(subj_dir, topdown=False):
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
            os.rmdir(subj_dir)
        except OSError:
            pass

    return True

