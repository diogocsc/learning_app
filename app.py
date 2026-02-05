# app.py
import os
from typing import List

import streamlit as st
import pandas as pd  # used in Progress tab

from models import QAItem, CardType
from db import (
    init_db,
    get_connection,
    load_all_cards,
    insert_card,
    add_subject,
    get_subjects,
    get_subject_id,
    record_attempt,
    get_card_stats,
    get_subject_stats,
    update_card_schedule,
    get_due_cards,
    create_user,
    get_user_by_username,
    insert_uploaded_file,
    get_uploaded_files,
    delete_uploaded_file_and_cards,
    delete_card,
    admin_log,
)

from config import UPLOAD_DIR, CUSTOM_CSS
from auth import show_auth_screen, hash_password, verify_password
from session_utils import init_session_state, add_manual_card
from card_generation import (
    extract_pages_from_pdf_bytes,
    chunk_page_text,
    generate_cards_from_chunk,
    normalize_text,
    generate_cards_from_pdf_path,
)
from admin_utils import get_username_by_id, delete_subject_and_data


def main():
    init_db()

    # If not logged in, show auth and stop
    if "real_user_id" not in st.session_state or "effective_user_id" not in st.session_state:
        show_auth_screen()
        return

    real_user_id = st.session_state["real_user_id"]
    effective_user_id = st.session_state["effective_user_id"]
    real_username = st.session_state.get("real_username", get_username_by_id(real_user_id))

    st.set_page_config(page_title="AI Learning Assistant", layout="wide")
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)
    st.title("📘 AI Learning Assistant – Subjects, PDFs, Flashcards & Quizzes")

    # Warning banner when impersonating
    if real_user_id != effective_user_id:
        effective_username = get_username_by_id(effective_user_id)
        st.warning(
            f"Admin impersonation active: you are logged in as **{real_username}** "
            f"but currently acting as **{effective_username}** (user ID {effective_user_id})."
        )

    # Initialize per-user state (for effective user)
    init_session_state(effective_user_id)

    # ---------- Sidebar: settings, profile, subjects, manual cards ----------
    with st.sidebar:
        # ------------------------- USER SESSION INFO -------------------------
        st.markdown("### 👤 Account")
        st.write(f"Signed in as **{real_username}**")

        if real_user_id != effective_user_id:
            effective_username = get_username_by_id(effective_user_id)
            st.info(f"Impersonating **{effective_username}**")

        if st.button("Log out"):
            for key in list(st.session_state.keys()):
                st.session_state.pop(key, None)
            st.rerun()

        st.markdown("---")

        # -------------------- PROFILE SETTINGS (Change Password) --------------------
        with st.expander("🔐 Profile Settings (Change Password)", expanded=False):
            old_pw = st.text_input("Current password", type="password", key="prof_old_pw")
            new_pw = st.text_input("New password", type="password", key="prof_new_pw")
            new_pw2 = st.text_input("Confirm new password", type="password", key="prof_new_pw2")

            if st.button("Update Password"):
                user = get_user_by_username(real_username)
                if not user or not verify_password(old_pw, user[2]):
                    st.error("Incorrect current password.")
                elif new_pw != new_pw2:
                    st.error("New passwords do not match.")
                elif len(new_pw) < 6:
                    st.error("Password must be at least 6 characters.")
                else:
                    pw_hash = hash_password(new_pw)
                    conn = get_connection()
                    cur = conn.cursor()
                    cur.execute("UPDATE users SET password_hash=? WHERE id=?", (pw_hash, real_user_id))
                    conn.commit()
                    conn.close()
                    st.success("Password updated!")

        st.markdown("---")

        # --------------------- SUBJECTS — MAIN SECTION ----------------------
        with st.expander("📚 Subjects (Select or Create)", expanded=True):
            subjects = get_subjects(effective_user_id)
            subject_names = [name for (_, name) in subjects]
            options = ["(Select subject)"] + subject_names + ["(Create new subject…)"]
            selected = st.selectbox("Current subject", options)

            if selected == "(Create new subject…)":
                new_subj = st.text_input("New subject name")
                if st.button("Add Subject"):
                    if new_subj.strip():
                        add_subject(new_subj.strip(), effective_user_id)
                        if real_user_id != effective_user_id:
                            admin_log(real_user_id, effective_user_id, f"Created subject '{new_subj.strip()}'")
                        st.success(f"Subject '{new_subj}' created.")
                        st.rerun()
            elif selected == "(Select subject)":
                st.session_state.current_subject_id = None
            else:
                subj_id = get_subject_id(selected, effective_user_id)
                st.session_state.current_subject_id = subj_id
                st.write(f"**Active subject:** {selected}")

            # Delete subject with confirmation
            current_subject_id = st.session_state.get("current_subject_id")
            if current_subject_id is not None:
                if st.button("🗑 Delete subject and data"):
                    st.session_state["confirm_delete_subject"] = True

                if st.session_state.get("confirm_delete_subject"):
                    st.warning("This deletes the subject, all cards, attempts, and uploaded files!")
                    c1, c2 = st.columns(2)
                    with c1:
                        if st.button("Confirm deletion"):
                            delete_subject_and_data(current_subject_id, effective_user_id, real_user_id)
                            st.session_state.current_subject_id = None
                            st.session_state.pop("confirm_delete_subject", None)
                            st.rerun()
                    with c2:
                        if st.button("Cancel"):
                            st.session_state.pop("confirm_delete_subject", None)
                            st.rerun()

        st.markdown("---")

        # -------------------- MANUAL CARDS — COLLAPSIBLE --------------------
        with st.expander("✍️ Add Manual Flashcards", expanded=False):
            manual_q = st.text_area("Question")
            manual_a = st.text_area("Answer")
            manual_type = st.selectbox(
                "Card type",
                ["flashcard", "short_answer", "fill_in_blank"],
                index=0,
            )

            if st.button("Add manual card"):
                if manual_q.strip() and manual_a.strip():
                    add_manual_card(
                        card_type=manual_type,
                        question=manual_q,
                        answer=manual_a,
                    )
                    if real_user_id != effective_user_id:
                        admin_log(real_user_id, effective_user_id, "Added manual card")
                else:
                    st.warning("Please provide both a question and an answer.")

        st.markdown("---")
        st.info(f"Total cards: **{len(st.session_state.deck)}**")

        # ----------------- ADMIN PANEL (impersonation & user management) -----------------
        if real_username == "admin":
            st.markdown("## 🔐 Admin Dashboard")

            conn = get_connection()
            cur = conn.cursor()
            cur.execute("SELECT id, username FROM users ORDER BY username")
            users = cur.fetchall()

            # Impersonation selector
            st.markdown("### Impersonate user")
            user_map = {uname: uid for uid, uname in users}
            if user_map:
                selected_user_for_imp = st.selectbox(
                    "Choose user to impersonate",
                    list(user_map.keys()),
                    key="imp_user_sel",
                )
                if st.button("Switch to this user", key="btn_impersonate"):
                    target_id = user_map[selected_user_for_imp]
                    st.session_state.effective_user_id = target_id
                    st.session_state.pop("deck_user_id", None)
                    st.success(f"Now impersonating user: {selected_user_for_imp} (id {target_id})")
                    st.experimental_rerun() if hasattr(st, "experimental_rerun") else st.rerun()
            else:
                st.info("No users found to impersonate.")

            if real_user_id != effective_user_id:
                if st.button("Stop impersonation (back to admin)", key="stop_imp"):
                    st.session_state.effective_user_id = real_user_id
                    st.session_state.pop("deck_user_id", None)
                    st.experimental_rerun() if hasattr(st, "experimental_rerun") else st.rerun()

            st.markdown("---")
            st.markdown("### User management")

            for uid, uname in users:
                st.markdown(f"#### User ID {uid}: {uname}")
                col1, col2, col3 = st.columns(3)

                # Rename user
                with col1:
                    new_name = st.text_input(
                        f"Rename {uname}",
                        value=uname,
                        key=f"rename_{uid}",
                    )
                    if st.button(f"Save username {uid}", key=f"btn_rename_{uid}"):
                        cur.execute("UPDATE users SET username=? WHERE id=?", (new_name, uid))
                        conn.commit()
                        admin_log(real_user_id, uid, f"Renamed user '{uname}' to '{new_name}'")
                        st.success("Username updated.")
                        st.experimental_rerun() if hasattr(st, "experimental_rerun") else st.rerun()

                # Change password
                with col2:
                    new_pw = st.text_input(
                        f"New password for {uname}",
                        type="password",
                        key=f"newpw_{uid}",
                    )
                    if st.button(f"Change password {uid}", key=f"btn_pw_{uid}"):
                        if len(new_pw) < 6:
                            st.error("Password must be at least 6 characters.")
                        else:
                            pw_hash = hash_password(new_pw)
                            cur.execute("UPDATE users SET password_hash=? WHERE id=?", (pw_hash, uid))
                            conn.commit()
                            admin_log(real_user_id, uid, "Admin changed user password")
                            st.success("Password changed.")
                            st.experimental_rerun() if hasattr(st, "experimental_rerun") else st.rerun()

                # Delete user
                with col3:
                    if uname == "admin":
                        st.write("(cannot delete admin)")
                    else:
                        delete_key = f"delete_user_{uid}"
                        confirm_key = f"confirm_delete_user_{uid}"

                        if st.button("Delete user", key=delete_key):
                            st.session_state[confirm_key] = True

                        if st.session_state.get(confirm_key):
                            st.warning(f"Delete user **{uname}** and all their data?")
                            c1, c2 = st.columns(2)

                            with c1:
                                if st.button("Yes, delete", key=f"yes_{delete_key}"):
                                    # Delete attempts
                                    cur.execute("DELETE FROM card_attempts WHERE user_id=?", (uid,))

                                    # Delete subjects and related data
                                    cur.execute("SELECT id FROM subjects WHERE user_id=?", (uid,))
                                    subj_rows = cur.fetchall()
                                    subj_ids = [r[0] for r in subj_rows]

                                    if subj_ids:
                                        placeholders = ",".join("?" for _ in subj_ids)
                                        # Delete cards for those subjects
                                        cur.execute(
                                            f"DELETE FROM cards WHERE subject_id IN ({placeholders})",
                                            subj_ids,
                                        )
                                        # Delete uploaded files for those subjects
                                        cur.execute(
                                            f"DELETE FROM uploaded_files "
                                            f"WHERE subject_id IN ({placeholders}) AND user_id=?",
                                            (*subj_ids, uid),
                                        )
                                        # Delete subjects
                                        cur.execute(
                                            f"DELETE FROM subjects WHERE id IN ({placeholders})",
                                            subj_ids,
                                        )

                                    # Delete remaining uploaded_files for this user (and physical files)
                                    cur.execute("SELECT stored_path FROM uploaded_files WHERE user_id=?", (uid,))
                                    for (stored_path,) in cur.fetchall():
                                        if stored_path and os.path.exists(stored_path):
                                            try:
                                                os.remove(stored_path)
                                            except OSError:
                                                pass
                                    cur.execute("DELETE FROM uploaded_files WHERE user_id=?", (uid,))

                                    # Delete admin logs involving this user
                                    cur.execute(
                                        "DELETE FROM admin_logs WHERE admin_id=? OR target_user_id=?",
                                        (uid, uid),
                                    )

                                    # Delete user
                                    cur.execute("DELETE FROM users WHERE id=?", (uid,))
                                    conn.commit()

                                    admin_log(real_user_id, uid, "Deleted user account")
                                    st.success("User deleted.")
                                    st.session_state.pop(confirm_key, None)
                                    st.experimental_rerun() if hasattr(st, "experimental_rerun") else st.rerun()

                            with c2:
                                if st.button("Cancel", key=f"cancel_{delete_key}"):
                                    st.session_state.pop(confirm_key, None)
                                    st.experimental_rerun() if hasattr(st, "experimental_rerun") else st.rerun()

            # Recent admin logs
            st.markdown("---")
            st.markdown("### Recent admin actions")
            cur.execute(
                """
                SELECT admin_id, target_user_id, action, timestamp
                FROM admin_logs
                ORDER BY id DESC
                LIMIT 50
                """
            )
            logs = cur.fetchall()
            conn.close()

            if logs:
                for admin_id, target_user_id, action, ts in logs:
                    st.write(
                        f"[{ts}] Admin {get_username_by_id(admin_id)} → "
                        f"{get_username_by_id(target_user_id)}: {action}"
                    )
            else:
                st.info("No admin actions recorded yet.")

    # --------------------------- Main layout ---------------------------
    st.markdown(
        """
        Upload PDFs for the **active subject**, generate flashcards & quiz questions,
        then review them in the study area below.
        """
    )

    # convenience alias
    user_id = effective_user_id

    # 1. Upload PDFs & generate (expander)
    with st.expander("📥 Manage PDFs & Generate Cards", expanded=False):
        st.subheader("Upload PDFs for the active subject")

        if st.session_state.current_subject_id is None:
            st.warning("Please select or create a subject in the sidebar before uploading PDFs.")

        uploaded_files = st.file_uploader(
            "Upload one or more PDF files",
            type=["pdf"],
            accept_multiple_files=True,
        )

        if uploaded_files:
            st.write("Files selected:")
            for f in uploaded_files:
                st.write(f"- {f.name}")

            max_cards = st.number_input(
                "Max cards per generation run",
                min_value=1,
                max_value=500,
                value=st.session_state.max_cards,
                step=5,
            )
            st.session_state.max_cards = int(max_cards)

            if st.button("🚀 Generate flashcards & quizzes from PDFs") and uploaded_files:
                subject_id = st.session_state.get("current_subject_id")
                if subject_id is None:
                    st.error("You must select a subject before generating cards.")
                else:
                    max_cards = st.session_state.get("max_cards", 50)

                    with st.spinner(f"Reading PDFs and generating up to {max_cards} cards..."):
                        deck: List[QAItem] = st.session_state.deck
                        current_id = max((c.id for c in deck), default=0) + 1
                        new_cards_count = 0

                        # Existing normalized questions for this subject (for dedup)
                        existing_norm_questions = [
                            normalize_text(c.question) for c in deck if c.subject_id == subject_id
                        ]

                        for file in uploaded_files:
                            if new_cards_count >= max_cards:
                                break

                            pdf_name = file.name
                            file_bytes = file.read()

                            # Save file to disk & record in DB
                            user_folder = UPLOAD_DIR / f"user_{user_id}" / f"subject_{subject_id}"
                            user_folder.mkdir(parents=True, exist_ok=True)
                            stored_path = user_folder / pdf_name

                            with open(stored_path, "wb") as f_out:
                                f_out.write(file_bytes)

                            insert_uploaded_file(user_id, subject_id, pdf_name, str(stored_path))
                            if real_user_id != user_id:
                                admin_log(real_user_id, user_id, f"Uploaded file {pdf_name}")

                            pages = extract_pages_from_pdf_bytes(file_bytes)

                            for page_info in pages:
                                if new_cards_count >= max_cards:
                                    break

                                page_number = page_info["page"]
                                text = page_info["text"]
                                if not text.strip():
                                    continue

                                chunks = chunk_page_text(text)

                                for chunk in chunks:
                                    if new_cards_count >= max_cards:
                                        break

                                    remaining = max_cards - new_cards_count
                                    per_chunk_limit = min(7, remaining)

                                    new_items = generate_cards_from_chunk(
                                        chunk_text=chunk,
                                        page=page_number,
                                        source_pdf=pdf_name,
                                        subject_id=subject_id,
                                        starting_id=current_id,
                                        max_items_for_this_chunk=per_chunk_limit,
                                        existing_norm_questions=existing_norm_questions,
                                    )

                                    if len(new_items) > remaining:
                                        new_items = new_items[:remaining]

                                    for card in new_items:
                                        insert_card(card)
                                        deck.append(card)

                                    new_cards_count += len(new_items)
                                    current_id += len(new_items)

                        st.success(
                            f"Generation complete! Added {new_cards_count} new cards. "
                            f"Your deck now has {len(st.session_state.deck)} cards."
                        )

                        if real_user_id != user_id:
                            admin_log(
                                real_user_id,
                                user_id,
                                f"Generated {new_cards_count} cards from uploaded PDFs",
                            )

    # 1b. Uploaded files & content management
    with st.expander("📂 Uploaded files & content management", expanded=False):
        current_subject_id = st.session_state.get("current_subject_id")

        if current_subject_id is None:
            st.info("Select a subject in the sidebar to see its uploaded files.")
        else:
            st.subheader("Uploaded PDFs for this subject")
            files = get_uploaded_files(user_id, current_subject_id)

            if not files:
                st.info("No files uploaded yet for this subject.")
            else:
                max_new_cards = st.number_input(
                    "Max new cards per file when generating more questions",
                    min_value=1,
                    max_value=500,
                    value=50,
                    step=5,
                    key="max_new_cards_per_file",
                )

                for fmeta in files:
                    col1, col2, col3, col4, col5 = st.columns([3, 2, 2, 2, 2])

                    with col1:
                        st.write(f"**{fmeta['filename']}**")
                        st.caption(f"Uploaded at: {fmeta['uploaded_at']}")

                    with col2:
                        count = sum(
                            1
                            for c in st.session_state.deck
                            if c.subject_id == current_subject_id
                            and c.source_pdf == fmeta["filename"]
                        )
                        st.write(f"Cards from this file: {count}")

                    with col3:
                        if st.button("Generate more questions", key=f"regen_{fmeta['id']}"):
                            added = generate_cards_from_pdf_path(
                                pdf_path=fmeta["stored_path"],
                                pdf_name=fmeta["filename"],
                                subject_id=current_subject_id,
                                max_new_cards=max_new_cards,
                            )
                            st.success(f"Added {added} new cards from {fmeta['filename']}.")
                            if real_user_id != user_id:
                                admin_log(
                                    real_user_id,
                                    user_id,
                                    f"Generated {added} new cards from {fmeta['filename']}",
                                )
                            st.experimental_rerun() if hasattr(st, "experimental_rerun") else st.rerun()

                    with col4:
                        # Download button
                        try:
                            with open(fmeta["stored_path"], "rb") as f_in:
                                st.download_button(
                                    "Download",
                                    data=f_in,
                                    file_name=fmeta["filename"],
                                    mime="application/pdf",
                                    key=f"dl_{fmeta['id']}",
                                )
                        except FileNotFoundError:
                            st.error("File missing on disk.")

                    with col5:
                        delete_key = f"del_file_{fmeta['id']}"
                        confirm_key = f"confirm_del_file_{fmeta['id']}"

                        if st.button("Delete file", key=delete_key):
                            st.session_state[confirm_key] = True

                        if st.session_state.get(confirm_key):
                            st.warning(f"Delete **{fmeta['filename']}** and all its cards?")
                            c1, c2 = st.columns(2)

                            with c1:
                                if st.button("Yes, delete", key=f"yes_{delete_key}"):
                                    stored_path = delete_uploaded_file_and_cards(fmeta["id"], user_id)
                                    if stored_path and os.path.exists(stored_path):
                                        try:
                                            os.remove(stored_path)
                                        except OSError:
                                            st.warning(
                                                "File metadata deleted, but physical file could not be removed."
                                            )

                                    st.success(f"Deleted {fmeta['filename']} and its cards.")
                                    if real_user_id != user_id:
                                        admin_log(
                                            real_user_id,
                                            user_id,
                                            f"Deleted file {fmeta['filename']} and its cards",
                                        )
                                    st.session_state.pop(confirm_key, None)
                                    st.experimental_rerun() if hasattr(st, "experimental_rerun") else st.rerun()

                            with c2:
                                if st.button("Cancel", key=f"cancel_{delete_key}"):
                                    st.session_state.pop(confirm_key, None)
                                    st.experimental_rerun() if hasattr(st, "experimental_rerun") else st.rerun()

    # ------------------------- Study Area -------------------------
    st.header("🎯 Study Area")

    if not st.session_state.deck:
        st.info("No cards yet. Upload PDFs and/or add manual cards to start studying.")
        return

    current_subject_id = st.session_state.get("current_subject_id")
    if current_subject_id is None:
        st.warning("Select a subject to study its cards.")
        return

    tab_flashcards, tab_quiz, tab_progress = st.tabs(["Flashcards", "Quiz", "Progress"])

    # ---------- Flashcards Tab (SRS) ----------
    with tab_flashcards:
        due_cards = get_due_cards(current_subject_id, limit=100)

        if not due_cards:
            st.success("🎉 No cards due right now for this subject!")
        else:
            st.subheader("Spaced Repetition – Due Cards")

            if "srs_index" not in st.session_state:
                st.session_state.srs_index = 0

            idx = st.session_state.srs_index
            if idx >= len(due_cards):
                st.session_state.srs_index = 0
                idx = 0

            card = due_cards[idx]

            st.markdown(f"**Card {idx + 1} of {len(due_cards)} due**")
            st.markdown(
                f"""
                <div style="border:1px solid #ccc; padding:1rem; border-radius:0.5rem;">
                <strong>Q:</strong> {card.question}
                </div>
                """,
                unsafe_allow_html=True,
            )
            st.caption(f"Type: {card.card_type}, Source: {card.source_pdf}, page {card.page}")

            if st.button("Show answer", key="show_srs_answer"):
                st.session_state.show_answer = True

            if st.session_state.show_answer:
                st.markdown(
                    f"""
                    <div style="border:1px solid #ccc; padding:1rem; border-radius:0.5rem; background:#f8f8f8;">
                    <strong>A:</strong> {card.answer}
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

                st.markdown("### How well did you remember this?")
                cols = st.columns(6)
                qualities = [0, 1, 2, 3, 4, 5]
                labels = ["0 (Null)", "1", "2", "3 (OK)", "4 (Good)", "5 (Perfect)"]

                for col, q, label in zip(cols, qualities, labels):
                    with col:
                        if st.button(label, key=f"quality_{card.id}_{q}"):
                            is_correct = q >= 3
                            record_attempt(card.id, card.subject_id, user_id, is_correct, q)
                            update_card_schedule(card.id, q)
                            st.session_state.show_answer = False
                            st.session_state.srs_index = (idx + 1) % len(due_cards)
                            st.experimental_rerun() if hasattr(st, "experimental_rerun") else st.rerun()

    # ---------- Quiz Tab (one question at a time) ----------
    with tab_quiz:
        st.subheader("Quiz Mode")

        quiz_items = [
            c
            for c in st.session_state.deck
            if c.card_type in ("short_answer", "fill_in_blank", "multiple_choice")
            and c.subject_id == current_subject_id
        ]

        if not quiz_items:
            st.info("No quiz questions for this subject yet.")
        else:
            if st.session_state.quiz_index >= len(quiz_items):
                st.session_state.quiz_index = len(quiz_items) - 1
            if st.session_state.quiz_index < 0:
                st.session_state.quiz_index = 0

            q_idx = st.session_state.quiz_index
            card = quiz_items[q_idx]

            with st.container():
                col_left, col_right = st.columns([3, 1])
                with col_left:
                    st.markdown(
                        f"**Question {q_idx + 1} of {len(quiz_items)} "
                        f"({card.card_type.replace('_', ' ').title()})**"
                    )
                with col_right:
                    st.progress((q_idx + 1) / len(quiz_items))

            with st.container():
                st.markdown(
                    f"""
                    <div style="border:1px solid #ccc; padding:1rem; border-radius:0.5rem;">
                    <strong>Q:</strong> {card.question}
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

                prev_answer = st.session_state.quiz_answers.get(card.id, "")

                if card.card_type == "multiple_choice" and card.options:
                    default_index = 0
                    if prev_answer in card.options:
                        default_index = card.options.index(prev_answer)

                    selected = st.radio(
                        "Choose an option:",
                        card.options,
                        index=default_index,
                        key=f"mcq_{card.id}",
                    )
                    user_answer = selected
                else:
                    user_answer = st.text_input(
                        "Your answer:",
                        value=prev_answer,
                        key=f"user_answer_{card.id}",
                    )

                col_prev, col_check, col_next = st.columns([1, 1, 1])
                feedback_placeholder = st.empty()

                with col_check:
                    if st.button("Check answer", key=f"check_{card.id}"):
                        st.session_state.quiz_answers[card.id] = user_answer

                        if card.card_type == "multiple_choice" and card.options:
                            is_correct = user_answer.strip() == card.answer.strip()
                        else:
                            is_correct = user_answer.strip().lower() == card.answer.strip().lower()

                        q_quality = 4 if is_correct else 2
                        record_attempt(card.id, card.subject_id, user_id, is_correct, q_quality)

                        if is_correct:
                            feedback_placeholder.success("✅ Correct!")
                        else:
                            feedback_placeholder.error("❌ Incorrect.")

                        feedback_placeholder.markdown(f"**Correct answer:** {card.answer}")
                        st.caption(f"Source: {card.source_pdf}, page {card.page}")

                with col_prev:
                    if st.button("⬅ Previous", disabled=(q_idx == 0)):
                        st.session_state.quiz_index = max(0, q_idx - 1)
                        st.experimental_rerun() if hasattr(st, "experimental_rerun") else st.rerun()

                with col_next:
                    if st.button("Next ➡", disabled=(q_idx == len(quiz_items) - 1)):
                        st.session_state.quiz_index = min(len(quiz_items) - 1, q_idx + 1)
                        st.experimental_rerun() if hasattr(st, "experimental_rerun") else st.rerun()

    # ---------- Progress Tab ----------
    with tab_progress:
        st.subheader("📊 Learning Progress")

        subject_id = st.session_state.current_subject_id
        if subject_id is None:
            st.info("Select a subject to see your stats.")
        else:
            total, correct = get_subject_stats(subject_id, user_id)
            accuracy = (correct / total * 100) if total > 0 else 0

            st.metric("Total Attempts", total)
            st.metric("Correct Answers", correct)
            st.metric("Accuracy", f"{accuracy:.1f}%")

            st.markdown("### Card Performance")

            rows = []
            for c in st.session_state.deck:
                if c.subject_id == subject_id:
                    t, a = get_card_stats(c.id, user_id)
                    acc = (a / t * 100) if t > 0 else None
                    rows.append(
                        {
                            "Card ID": c.id,
                            "Question": c.question[:80] + "...",
                            "Attempts": t,
                            "Correct": a,
                            "Accuracy %": acc,
                        }
                    )

            df = pd.DataFrame(rows)
            st.dataframe(df)

            # Card deletion UI with confirmation
            st.markdown("### Delete individual cards")
            for c in st.session_state.deck:
                if c.subject_id != subject_id:
                    continue

                col_q, col_btn = st.columns([5, 1])
                with col_q:
                    st.write(f"#{c.id} [{c.card_type}] {c.question[:80]}...")
                with col_btn:
                    delete_key = f"del_card_{c.id}"
                    confirm_key = f"confirm_card_{c.id}"

                    if st.button("Delete", key=delete_key):
                        st.session_state[confirm_key] = True

                    if st.session_state.get(confirm_key):
                        st.warning(f"Delete card #{c.id}?")
                        col_y, col_n = st.columns(2)

                        with col_y:
                            if st.button("Yes, delete", key=f"yes_{delete_key}"):
                                ok = delete_card(c.id, user_id)
                                if ok:
                                    st.session_state.deck = [d for d in st.session_state.deck if d.id != c.id]
                                    st.success(f"Card {c.id} deleted.")
                                    if real_user_id != user_id:
                                        admin_log(real_user_id, user_id, f"Deleted card {c.id}")
                                else:
                                    st.error("Could not delete card.")
                                st.session_state.pop(confirm_key, None)
                                st.experimental_rerun() if hasattr(st, "experimental_rerun") else st.rerun()

                        with col_n:
                            if st.button("Cancel", key=f"cancel_{delete_key}"):
                                st.session_state.pop(confirm_key, None)
                                st.experimental_rerun() if hasattr(st, "experimental_rerun") else st.rerun()


if __name__ == "__main__":
    main()