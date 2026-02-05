# admin_utils.py
import os

import streamlit as st

from db import get_connection, admin_log


def get_username_by_id(user_id: int) -> str:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT username FROM users WHERE id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else f"user_{user_id}"


def delete_subject_and_data(subject_id: int, effective_user_id: int, real_user_id: int):
    """
    Delete a subject and all its cards, attempts, and uploaded files for the effective user.
    """
    conn = get_connection()
    cur = conn.cursor()

    # Get subject name for logging
    cur.execute(
        "SELECT name FROM subjects WHERE id = ? AND user_id = ?",
        (subject_id, effective_user_id),
    )
    row = cur.fetchone()
    if not row:
        conn.close()
        st.error("Subject not found or does not belong to this user.")
        return

    subj_name = row[0]

    # Get all cards for this subject
    cur.execute("SELECT id FROM cards WHERE subject_id = ?", (subject_id,))
    card_ids = [r[0] for r in cur.fetchall()]

    # Delete attempts for these cards and this user
    if card_ids:
        placeholders = ",".join("?" * len(card_ids))
        cur.execute(
            f"DELETE FROM card_attempts "
            f"WHERE card_id IN ({placeholders}) AND user_id = ?",
            (*card_ids, effective_user_id),
        )

        # Delete cards
        cur.execute(
            f"DELETE FROM cards WHERE id IN ({placeholders})",
            card_ids,
        )

    # Delete uploaded_files rows and physical files
    cur.execute(
        "SELECT stored_path FROM uploaded_files WHERE user_id = ? AND subject_id = ?",
        (effective_user_id, subject_id),
    )
    file_rows = cur.fetchall()
    for (stored_path,) in file_rows:
        if stored_path and os.path.exists(stored_path):
            try:
                os.remove(stored_path)
            except OSError:
                pass

    cur.execute(
        "DELETE FROM uploaded_files WHERE user_id = ? AND subject_id = ?",
        (effective_user_id, subject_id),
    )

    # Finally delete subject
    cur.execute(
        "DELETE FROM subjects WHERE id = ? AND user_id = ?",
        (subject_id, effective_user_id),
    )

    conn.commit()
    conn.close()

    # Log admin action if impersonating
    if real_user_id != effective_user_id:
        admin_log(
            real_user_id,
            effective_user_id,
            f"Deleted subject '{subj_name}' (id={subject_id}) and all its data.",
        )