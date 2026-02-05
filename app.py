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
    update_excluded_pages,
    get_excluded_pages_map,
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
from rag_store import add_documents

from admin_pages import render_admin_users  # import at top of file


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

    # Routing: which view to show
    if "view" not in st.session_state:
        st.session_state.view = "main"  # default view

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

        # Admin-only link
        if real_username == "admin":
            if st.button("🔐 Admin User Management", key="go_admin_users"):
                st.session_state.view = "admin_users"
                st.rerun()
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
    # === ROUTER: decide what to render ===
    view = st.session_state.get("view", "main")

    if view == "admin_users":
        # Guard: only allow real admin
        if real_username != "admin":
            st.error("You must be admin to access this page.")
            st.stop()
        # Render admin users page
        render_admin_users(real_user_id)
        return

    # --------------------------- Main layout ---------------------------
    st.markdown(
        """
        Upload PDFs for the **active subject**, generate flashcards & quiz questions,
        then review them in the study area below.
        """
    )

    # convenience alias
    user_id = effective_user_id

    # 1. Upload PDFs & generate (expander) — REWRITTEN
    with st.expander("📥 Manage PDFs & Generate Cards", expanded=False):
        st.subheader("Upload PDFs for the active subject")

        if st.session_state.current_subject_id is None:
            st.warning("Please select or create a subject in the sidebar before uploading PDFs.")

        uploaded_files = st.file_uploader(
            "Upload one or more PDF files",
            type=["pdf"],
            accept_multiple_files=True,
            key="uploader_manage_generate"
        )

        # Let the user control the card budget for this run
        max_cards_input = st.number_input(
            "Max cards per generation run",
            min_value=1,
            max_value=500,
            value=st.session_state.get("max_cards", 50),
            step=5,
            key="max_cards_per_run"
        )
        # Persist in session_state (separate key from widget key is OK)
        st.session_state.max_cards = int(max_cards_input)

        # If user selected files, show a pre-generation "exclude pages" UI per file
        if uploaded_files:
            st.write("Files selected:")
            for f in uploaded_files:
                st.write(f"- {f.name}")

                # Use TWO KEYS to avoid Streamlit error:
                # - ui_key: widget's key
                # - logic_key: your own state for business logic
                ui_key = f"ui_exclude_pages_{f.name}"
                logic_key = f"exclude_pre_{f.name}"

                # The widget manages ui_key internally; we read its value and copy to logic_key
                val = st.text_input(
                    f"Exclude pages BEFORE generation for {f.name} (e.g. 1,2,5-7)",
                    value=st.session_state.get(logic_key, ""),
                    key=ui_key
                )
                # Store separately (safe because logic_key != ui_key)
                st.session_state[logic_key] = val

            # Generation button
            if st.button("🚀 Generate flashcards & quizzes from PDFs", key="btn_generate_from_uploads"):
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
                            normalize_text(c.question)
                            for c in deck
                            if c.subject_id == subject_id
                        ]

                        for file in uploaded_files:
                            if new_cards_count >= max_cards:
                                break

                            pdf_name = file.name
                            file_bytes = file.read()

                            # 1) Save PDF to disk
                            user_folder = UPLOAD_DIR / f"user_{user_id}" / f"subject_{subject_id}"
                            user_folder.mkdir(parents=True, exist_ok=True)
                            stored_path = user_folder / pdf_name
                            with open(stored_path, "wb") as f_out:
                                f_out.write(file_bytes)

                            # 2) Insert metadata FIRST so we get file_id
                            file_id = insert_uploaded_file(
                                user_id,
                                subject_id,
                                pdf_name,
                                str(stored_path)
                            )

                            # 3) Persist pre-generation exclusions provided by the user
                            pre_key = f"exclude_pre_{pdf_name}"
                            user_excluded_text = (st.session_state.get(pre_key, "") or "").strip()
                            if user_excluded_text:
                                # Save in DB; your DB layer parses and stores ranges
                                update_excluded_pages(file_id, user_excluded_text)

                            # 4) Load excluded pages map (int set) for this file_id
                            excluded_pages = get_excluded_pages_map(file_id)

                            # 5) Extract pages and generate cards while skipping excluded ones
                            pages = extract_pages_from_pdf_bytes(file_bytes)
                            rag_chunks = []
                            for page_info in pages:
                                if new_cards_count >= max_cards:
                                    break

                                page_number = page_info["page"]

                                # Skip excluded pages
                                if page_number in excluded_pages:
                                    continue

                                text = page_info["text"]
                                if not text.strip():
                                    continue

                                # Split into chunks and collect for RAG
                                chunks = chunk_page_text(text)
                                rag_chunks.extend(chunks)

                                # Generate cards per chunk (respecting budget)
                                for chunk in chunks:
                                    if new_cards_count >= max_cards:
                                        break
                                    per_chunk_limit = min(7, max_cards - new_cards_count)
                                    new_items = generate_cards_from_chunk(
                                        chunk_text=chunk,
                                        page=page_number,
                                        source_pdf=pdf_name,
                                        subject_id=subject_id,
                                        starting_id=current_id,
                                        max_items_for_this_chunk=per_chunk_limit,
                                        existing_norm_questions=existing_norm_questions,
                                    )

                                    # Persist cards
                                    for card in new_items:
                                        insert_card(card)
                                        deck.append(card)
                                    new_cards_count += len(new_items)
                                    current_id += len(new_items)

                            # 6) Index allowed chunks in RAG (after filtering)
                            if rag_chunks:
                                add_documents(user_id, subject_id, rag_chunks)

                        st.success(
                            f"Generation complete! Added {new_cards_count} new cards. "
                            f"Your deck now has {len(st.session_state.deck)} cards."
                        )

                        # Impersonation log if applicable
                        if real_user_id != user_id:
                            admin_log(
                                real_user_id,
                                user_id,
                                f"Generated {new_cards_count} cards from uploaded PDFs (with pre-gen exclusions)"
                            )

    # 1b. Uploaded files & content management — FULLY REWRITTEN
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
                    key="manage_max_new_cards_per_file"
                )

                for fmeta in files:
                    file_id = fmeta["id"]
                    filename = fmeta["filename"]
                    stored_path = fmeta["stored_path"]

                    col1, col2, col3, col4, col5 = st.columns([3, 2, 2, 2, 2])

                    # ----- FILE NAME + Upload time -----
                    with col1:
                        st.write(f"**{filename}**")
                        st.caption(f"Uploaded at: {fmeta['uploaded_at']}")

                    # ----- CARD COUNT FOR THIS FILE -----
                    with col2:
                        count = sum(
                            1 for c in st.session_state.deck
                            if c.subject_id == current_subject_id
                            and c.source_pdf == filename
                        )
                        st.write(f"Cards: {count}")

                    # ----- EXCLUDED PAGES UI (safe dual-key pattern) -----
                    with col3:
                        # DB value as displayed default
                        current_excl_str = fmeta.get("excluded_pages", "") or ""

                        # Widget key (UI)
                        ui_key = f"ui_file_excl_{file_id}"
                        # Logic key (separate internal value)
                        logic_key = f"file_excl_logic_{file_id}"

                        val = st.text_input(
                            f"Exclude pages (e.g. 1,2,4-6)",
                            value=current_excl_str if logic_key not in st.session_state else st.session_state[
                                logic_key],
                            key=ui_key
                        )

                        # Save the mirrored internal logic key
                        st.session_state[logic_key] = val

                        if st.button("Save exclusions", key=f"save_excl_{file_id}"):
                            update_excluded_pages(file_id, val)
                            st.success("Saved excluded pages")
                            st.rerun()

                    # ----- REGENERATE QUESTIONS BUTTON -----
                    with col3:
                        pass  # Already used above but layout is okay

                    with col4:
                        if st.button("Generate more questions", key=f"regen_more_{file_id}"):
                            # 1. Load PDF bytes
                            try:
                                with open(stored_path, "rb") as pdf_file:
                                    file_bytes = pdf_file.read()
                            except FileNotFoundError:
                                st.error("PDF file not found on disk.")
                                continue

                            # 2. Parse exclusions (from DB)
                            excluded_pages = get_excluded_pages_map(file_id)

                            # 3. Extract + chunk text, skipping excluded pages
                            pages = extract_pages_from_pdf_bytes(file_bytes)
                            all_chunks = []

                            for page_info in pages:
                                page_num = page_info["page"]

                                if page_num in excluded_pages:
                                    continue

                                text = page_info["text"]
                                if not text.strip():
                                    continue

                                chunks = chunk_page_text(text)
                                all_chunks.extend(chunks)

                            # 4. Add chunks to RAG
                            if all_chunks:
                                add_documents(
                                    user_id=user_id,
                                    subject_id=current_subject_id,
                                    docs=all_chunks
                                )

                            # 5. Generate new cards (your existing utility)
                            added = generate_cards_from_pdf_path(
                                pdf_path=stored_path,
                                pdf_name=filename,
                                subject_id=current_subject_id,
                                max_new_cards=max_new_cards,
                                file_id=file_id
                            )

                            st.success(f"Added {added} new cards from {filename}.")

                            if real_user_id != user_id:
                                admin_log(
                                    real_user_id,
                                    user_id,
                                    f"Generated {added} new cards from {filename} (via management section)"
                                )

                            st.rerun()

                    # ----- DOWNLOAD PDF BUTTON -----
                    with col5:
                        try:
                            with open(stored_path, "rb") as f_in:
                                st.download_button(
                                    "Download",
                                    data=f_in,
                                    file_name=filename,
                                    mime="application/pdf",
                                    key=f"dl_file_{file_id}",
                                )
                        except FileNotFoundError:
                            st.error("File missing on disk.")

                    # ----- DELETE PDF AND ITS CARDS -----
                    with col5:
                        delete_key = f"del_file_{file_id}"
                        confirm_key = f"confirm_del_file_{file_id}"

                        if st.button("Delete file", key=delete_key):
                            st.session_state[confirm_key] = True

                        if st.session_state.get(confirm_key):
                            st.warning(f"Delete **{filename}** and all its cards?")
                            c1, c2 = st.columns(2)

                            with c1:
                                if st.button("Yes, delete", key=f"yes_{delete_key}"):
                                    deleted_path = delete_uploaded_file_and_cards(file_id, user_id)

                                    # Remove from disk
                                    if deleted_path and os.path.exists(deleted_path):
                                        try:
                                            os.remove(deleted_path)
                                        except OSError:
                                            st.warning("Metadata deleted but file could not be removed.")

                                    st.success(f"Deleted {filename} and its cards.")

                                    if real_user_id != user_id:
                                        admin_log(
                                            real_user_id,
                                            user_id,
                                            f"Deleted file {filename} and its cards"
                                        )

                                    st.session_state.pop(confirm_key, None)
                                    st.rerun()

                            with c2:
                                if st.button("Cancel", key=f"cancel_{delete_key}"):
                                    st.session_state.pop(confirm_key, None)
                                    st.rerun()
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

        # Filter out multiple‑choice cards, because they already show on quiz.
        due_cards = [c for c in due_cards if c.card_type != "multiple_choice"]

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