# app_old.py
import os
import json
from typing import List
from pathlib import Path
import re
import random
from difflib import SequenceMatcher
from datetime import datetime, timedelta

import streamlit as st
import fitz  # PyMuPDF
import bcrypt

import llm_client as llm
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
    admin_log,  # assumes admin_logs table exists
)

# Project root and uploads dir
PROJECT_ROOT = Path(__file__).resolve().parent
UPLOAD_DIR = PROJECT_ROOT / "data" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Rate limiting for registrations (per browser session)
MAX_REG_ATTEMPTS = 5
REG_RATE_WINDOW = timedelta(minutes=10)

# =========================
# 0. Global CSS for layout
# =========================

CUSTOM_CSS = """
<style>
#MainMenu {visibility: hidden;}
footer {visibility: hidden;}

.block-container {
    max-width: 1200px;
    padding-top: 1rem;
    padding-bottom: 2rem;
}

.question-card {
    background-color: #f9fafb;
    border: 1px solid #e5e7eb;
    border-radius: 10px;
    padding: 1.5rem 2rem;
    margin-bottom: 1.5rem;
}

.question-text {
    font-size: 1.1rem;
    line-height: 1.6;
}

div[data-testid="stRadio"] label {
    font-size: 1.0rem !important;
}

div[data-testid="column"] > button {
    width: 100%;
}

.quiz-header {
    margin-bottom: 1rem;
}
</style>
"""

# =========================
# Helpers for deduplication
# =========================

def normalize_text(s: str) -> str:
    """Normalize text for deduplication: lowercase, trim, collapse internal spaces."""
    return re.sub(r"\s+", " ", s.strip().lower())


def is_similar_to_existing(norm_question: str, existing_norm_questions: List[str], threshold: float = 0.85) -> bool:
    """
    Check if norm_question is too similar to any existing normalized question.
    Uses difflib.SequenceMatcher for approximate (semantic-ish) similarity.
    threshold in [0, 1]; higher = stricter dedup.
    """
    for q in existing_norm_questions:
        if SequenceMatcher(None, norm_question, q).ratio() >= threshold:
            return True
    return False

# =========================
# 1. LLM Client (OpenAI-compatible)
# =========================

SYSTEM_PROMPT = """You are an assistant that creates study materials
(flashcards and quiz questions) from technical or educational text.
Output strictly valid JSON. No explanations.
JSON schema:
{
  "items": [
    {
      "card_type": "flashcard" | "short_answer" | "fill_in_blank" | "multiple_choice",
      "question": "string",
      "answer": "string",
      "options": ["A", "B", "C", "D"] | null
    },
    ...
  ]
}
Rules:
- For multiple_choice:
  - Provide 3 to 5 options in "options".
  - Exactly ONE option must be correct.
  - "answer" must contain the full text of the correct option (not just the letter).
- For flashcard, short_answer, and fill_in_blank, set "options" to null.
"""


def is_metadata_question(question: str, answer: str) -> bool:
    """
    Heuristically filter out questions that are about page numbers, titles,
    sections, etc., instead of conceptual content.
    """
    q = question.lower()
    a = answer.lower()

    forbidden_in_q = [
        "page number",
        "which page",
        "on what page",
        "page ",
        "section ",
        "chapter ",
        "figure ",
        "table ",
        "title of the",
        "title of this",
        "name of the article",
        "name of this article",
        "document title",
        "heading",
        "subheading",
    ]

    if any(k in q for k in forbidden_in_q):
        return True

    if a.startswith("page "):
        return True
    if a.startswith("p.") or a.startswith("pg."):
        return True
    if a.strip().isdigit():
        return True

    if len(a.split()) <= 3 and any(word in q for word in ["title", "name", "heading"]):
        return True

    return False


def generate_cards_from_chunk(
    chunk_text: str,
    page: int,
    source_pdf: str,
    subject_id: int,
    starting_id: int = 1,
    max_items_for_this_chunk: int = 7,
    existing_norm_questions: List[str] = None,
) -> List[QAItem]:
    """
    Calls the LLM to generate cards from a chunk of text.
    Returns a list of QAItem with subject_id filled in.

    existing_norm_questions: list of normalized question texts for this subject,
    used to avoid duplicates / near-duplicates.
    """
    if existing_norm_questions is None:
        existing_norm_questions = []

    user_prompt = f"""
Text (from page {page} of {source_pdf}):
\"\"\"{chunk_text}\"\"\" 

Create up to {max_items_for_this_chunk} items in total:
- Flashcards (Q/A)
- Short-answer questions
- Fill-in-the-blank questions (use '___' where the blank should be).
- Multiple-choice questions (3–5 options, exactly one correct).

Rules:
- Focus on the most important concepts and knowledge, not formatting or metadata.
- DO NOT create questions about:
  - Page numbers or which page something is on.
  - Section, chapter, or figure numbers.
  - The title of the document, article, chapter, or section.
  - Headings, subheadings, or other purely structural elements.
  - the document's authors or its terms.
- Answers must be accurate, concise, and self-contained.
- For multiple_choice:
  - Provide 3–5 options.
  - Exactly one option is correct.
  - The "answer" field must be the full correct option text.
- DO NOT include page numbers inside the question or answer text; they will be stored separately.
- Questions and answers must use the same language as the Text provided above (no translation).
Return only JSON in the schema specified.
"""
    response = llm.ask_question(SYSTEM_PROMPT + "\n" + user_prompt)
    content = response

    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        st.error(f"Failed to parse JSON from LLM on page {page}: {e}")
        st.text("Raw model output:")
        st.code(content)
        return []

    items: List[QAItem] = []
    idx = starting_id

    for item in data.get("items", []):
        card_type = item.get("card_type")
        question = item.get("question")
        answer = item.get("answer")
        options = item.get("options")

        if card_type not in ("flashcard", "short_answer", "fill_in_blank", "multiple_choice"):
            continue
        if not question or not answer:
            continue

        # Filter metadata-style questions
        if is_metadata_question(question, answer):
            continue

        # Semantic-ish deduplication by question text
        norm_q = normalize_text(question)
        if is_similar_to_existing(norm_q, existing_norm_questions, threshold=0.85):
            # Too close to an existing question for this subject
            continue

        if card_type == "multiple_choice":
            if not isinstance(options, list) or not all(isinstance(o, str) for o in options):
                continue
        else:
            options = None

        qa = QAItem(
            id=idx,
            card_type=item["card_type"],
            question=item["question"].strip(),
            answer=item["answer"].strip(),
            source_pdf=source_pdf,
            page=page,
            subject_id=subject_id,
            options=options,
        )
        items.append(qa)
        idx += 1

        # Add to existing_norm_questions to prevent near-duplicates within this run
        existing_norm_questions.append(norm_q)

    return items


# =========================
# 2. PDF handling & text chunking
# =========================

def extract_pages_from_pdf_bytes(file_bytes: bytes) -> List[dict]:
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    pages = []
    for i, page in enumerate(doc, start=1):
        text = page.get_text()
        pages.append({"page": i, "text": text})
    doc.close()
    return pages


def chunk_page_text(page_text: str, max_chars: int = 1200) -> List[str]:
    paragraphs = [p.strip() for p in page_text.split("\n") if p.strip()]
    chunks = []
    current = ""
    for p in paragraphs:
        if len(current) + len(p) + 1 <= max_chars:
            current += (" " if current else "") + p
        else:
            if current:
                chunks.append(current)
            current = p
    if current:
        chunks.append(current)
    return chunks


# =========================
# 3. Auth helpers (emoji CAPTCHA + rate limiting + impersonation)
# =========================

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def setup_emoji_captcha():
    """
    Initialize an emoji CAPTCHA:
      - Random category (animals, food, faces, objects)
      - Pick one target emoji & label, plus 3 distractors
    Stores in session_state:
        captcha_category
        captcha_target_label
        captcha_correct_emoji
        captcha_choices
        captcha_selected
    """
    categories = {
        "animals": [
            ("cat", "🐱"),
            ("dog", "🐶"),
            ("frog", "🐸"),
            ("monkey", "🐵"),
            ("panda", "🐼"),
            ("lion", "🦁"),
        ],
        "food": [
            ("pizza", "🍕"),
            ("apple", "🍎"),
            ("banana", "🍌"),
            ("cake", "🍰"),
            ("ice cream", "🍨"),
            ("burger", "🍔"),
        ],
        "faces": [
            ("smiling face", "😊"),
            ("laughing face", "😂"),
            ("crying face", "😢"),
            ("angry face", "😠"),
            ("winking face", "😉"),
            ("surprised face", "😲"),
        ],
        "objects": [
            ("car", "🚗"),
            ("airplane", "✈️"),
            ("book", "📚"),
            ("computer", "💻"),
            ("phone", "📱"),
            ("light bulb", "💡"),
        ],
    }

    category_name = random.choice(list(categories.keys()))
    options = categories[category_name]

    target_label, correct_emoji = random.choice(options)

    # Prefer distractors from same category
    distractors = [e for (label, e) in options if e != correct_emoji]

    # If not enough in same category, pull from others
    if len(distractors) >= 3:
        distractors = random.sample(distractors, 3)
    else:
        others = [
            e
            for cat, items in categories.items()
            for (label, e) in items
            if e not in [correct_emoji] + distractors
        ]
        needed = 3 - len(distractors)
        distractors += random.sample(others, needed)

    choices = distractors + [correct_emoji]
    random.shuffle(choices)

    st.session_state.captcha_category = category_name
    st.session_state.captcha_target_label = target_label
    st.session_state.captcha_correct_emoji = correct_emoji
    st.session_state.captcha_choices = choices
    st.session_state.captcha_selected = None


def show_auth_screen():
    st.set_page_config(page_title="AI Learning Assistant", layout="wide")
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)
    st.title("📘 AI Learning Assistant – Login / Register")

    tab_login, tab_register = st.tabs(["Login", "Register"])

    # ---- Login tab ----
    with tab_login:
        st.subheader("Login")
        username = st.text_input("Username", key="login_username")
        password = st.text_input("Password", type="password", key="login_password")

        if st.button("Login", key="btn_login"):
            if not username or not password:
                st.error("Please enter both username and password.")
            else:
                user = get_user_by_username(username)
                if user is None:
                    st.error("User not found.")
                else:
                    user_id, uname, pwd_hash = user
                    if verify_password(password, pwd_hash):
                        # real_user_id = who actually logged in
                        st.session_state.real_user_id = user_id
                        st.session_state.real_username = uname
                        # effective_user_id = whose data we're currently acting on
                        st.session_state.effective_user_id = user_id
                        st.success("Logged in successfully!")
                        st.experimental_rerun() if hasattr(st, "experimental_rerun") else st.rerun()
                    else:
                        st.error("Incorrect password.")

    # ---- Register tab ----
    with tab_register:
        st.subheader("Create a new account")

        # --- Rate limiting state (per session) ---
        now = datetime.utcnow()
        first = st.session_state.get("reg_rate_first", now)
        attempts = st.session_state.get("reg_rate_attempts", 0)

        # Reset window if expired
        if now - first > REG_RATE_WINDOW:
            first = now
            attempts = 0

        st.session_state["reg_rate_first"] = first
        st.session_state["reg_rate_attempts"] = attempts

        # Registration fields
        new_username = st.text_input("New username", key="reg_username")
        new_password = st.text_input("New password", type="password", key="reg_password")
        new_password2 = st.text_input("Confirm password", type="password", key="reg_password2")

        st.markdown("#### Human verification")

        # Initialize emoji CAPTCHA state if needed
        if "captcha_target_label" not in st.session_state:
            setup_emoji_captcha()

        target_label = st.session_state.captcha_target_label
        choices = st.session_state.captcha_choices

        st.write(f"Click the **{target_label}** to prove you're human:")

        cols = st.columns(len(choices))
        for idx, emoji in enumerate(choices):
            if cols[idx].button(emoji, key=f"captcha_btn_{emoji}_{idx}"):
                st.session_state.captcha_selected = emoji

        if st.session_state.get("captcha_selected"):
            st.info(f"You selected: {st.session_state.captcha_selected}")

        if st.button("Create account", key="create_account_btn"):
            now = datetime.utcnow()
            first = st.session_state.get("reg_rate_first", now)
            attempts = st.session_state.get("reg_rate_attempts", 0)

            # Reset window if expired
            if now - first > REG_RATE_WINDOW:
                first = now
                attempts = 0

            # Check limit
            if attempts >= MAX_REG_ATTEMPTS:
                remaining = REG_RATE_WINDOW - (now - first)
                remaining_minutes = max(1, int(remaining.total_seconds() // 60))
                st.error("Too many registration attempts. Please wait before trying again.")
                st.info(f"You can try again in up to {remaining_minutes} minutes.")
                st.session_state["reg_rate_first"] = first
                st.session_state["reg_rate_attempts"] = attempts
                return

            # Count this attempt
            attempts += 1
            st.session_state["reg_rate_first"] = first
            st.session_state["reg_rate_attempts"] = attempts

            # Basic validation
            if not new_username or not new_password:
                st.error("Username and password are required.")
                return
            if new_password != new_password2:
                st.error("Passwords do not match.")
                return
            if len(new_password) < 6:
                st.error("Please use a password with at least 6 characters.")
                return

            # CAPTCHA validation
            correct_emoji = st.session_state.captcha_correct_emoji
            selected = st.session_state.get("captcha_selected")

            if not selected:
                st.error("Please complete the human verification (click the correct emoji).")
                return
            if selected != correct_emoji:
                st.error("Human verification failed. Please try again.")
                # Reset CAPTCHA
                setup_emoji_captcha()
                return

            # All good: create user
            pw_hash = hash_password(new_password)
            ok = create_user(new_username, pw_hash)
            if ok:
                st.success("Account created! You can now log in.")
                # Reset CAPTCHA and rate limiting after success
                setup_emoji_captcha()
                st.session_state["reg_rate_attempts"] = 0
            else:
                st.error("Username already exists. Please choose another one.")
                # Optionally refresh CAPTCHA
                setup_emoji_captcha()


# =========================
# 4. Streamlit helpers
# =========================

def init_session_state(effective_user_id: int):
    # Reload deck when effective user changes
    if "deck_user_id" not in st.session_state or st.session_state.deck_user_id != effective_user_id:
        st.session_state.deck: List[QAItem] = load_all_cards(effective_user_id)
        st.session_state.deck_user_id = effective_user_id

    if "flashcard_index" not in st.session_state:
        st.session_state.flashcard_index = 0
    if "show_answer" not in st.session_state:
        st.session_state.show_answer = False
    if "current_subject_id" not in st.session_state:
        st.session_state.current_subject_id = None
    if "max_cards" not in st.session_state:
        st.session_state.max_cards = 50  # default limit per generation run
    if "quiz_index" not in st.session_state:
        st.session_state.quiz_index = 0
    if "quiz_answers" not in st.session_state:
        st.session_state.quiz_answers = {}


def add_manual_card(card_type: CardType, question: str, answer: str):
    subject_id = st.session_state.get("current_subject_id")
    if subject_id is None:
        st.error("Please select a subject before adding manual cards.")
        return

    deck: List[QAItem] = st.session_state.deck
    new_id = max((c.id for c in deck), default=0) + 1
    card = QAItem(
        id=new_id,
        card_type=card_type,
        question=question.strip(),
        answer=answer.strip(),
        source_pdf="manual",
        page=0,
        subject_id=subject_id,
    )
    insert_card(card)
    deck.append(card)
    st.success("Manual card added to the deck.")


def generate_cards_from_pdf_path(pdf_path: str, pdf_name: str, subject_id: int, max_new_cards: int) -> int:
    """
    Read a PDF from disk and generate up to max_new_cards cards for the given subject.
    Returns the number of new cards added.
    """
    with open(pdf_path, "rb") as f:
        file_bytes = f.read()

    pages = extract_pages_from_pdf_bytes(file_bytes)
    deck: List[QAItem] = st.session_state.deck
    current_id = max((c.id for c in deck), default=0) + 1
    new_cards_count = 0

    # Existing normalized questions for this subject (for dedup)
    existing_norm_questions = [
        normalize_text(c.question)
        for c in deck
        if c.subject_id == subject_id
    ]

    for page_info in pages:
        if new_cards_count >= max_new_cards:
            break

        page_number = page_info["page"]
        text = page_info["text"]
        if not text.strip():
            continue

        chunks = chunk_page_text(text)
        for chunk in chunks:
            if new_cards_count >= max_new_cards:
                break

            remaining = max_new_cards - new_cards_count
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

    return new_cards_count


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
    cur.execute("SELECT name FROM subjects WHERE id = ? AND user_id = ?", (subject_id, effective_user_id))
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
        cur.execute(
            f"DELETE FROM card_attempts WHERE card_id IN ({','.join('?'*len(card_ids))}) AND user_id = ?",
            (*card_ids, effective_user_id),
        )
        # Delete cards
        cur.execute(
            f"DELETE FROM cards WHERE id IN ({','.join('?'*len(card_ids))})",
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
        admin_log(real_user_id, effective_user_id, f"Deleted subject '{subj_name}' (id={subject_id}) and all its data.")


# =========================
# 5. Main Streamlit app
# =========================

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
        # ------------------------------
        # USER SESSION INFO
        # ------------------------------
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

        # ------------------------------
        # PROFILE SETTINGS (COLLAPSIBLE)
        # ------------------------------
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

        # ------------------------------
        # SUBJECTS — MAIN SECTION
        # ------------------------------
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
                            admin_log(real_user_id, effective_user_id,
                                      f"Created subject '{new_subj.strip()}'")
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
                    st.warning(
                        "This deletes the subject, all cards, attempts, and uploaded files!"
                    )
                    c1, c2 = st.columns(2)
                    with c1:
                        if st.button("Confirm deletion"):
                            delete_subject_and_data(
                                current_subject_id, effective_user_id, real_user_id
                            )
                            st.session_state.current_subject_id = None
                            st.session_state.pop("confirm_delete_subject", None)
                            st.rerun()
                    with c2:
                        if st.button("Cancel"):
                            st.session_state.pop("confirm_delete_subject", None)
                            st.rerun()

        st.markdown("---")

        # ------------------------------
        # MANUAL CARDS — COLLAPSIBLE
        # ------------------------------
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
                        card_type=manual_type, question=manual_q, answer=manual_a
                    )
                    if real_user_id != effective_user_id:
                        admin_log(real_user_id, effective_user_id, "Added manual card")
                else:
                    st.warning("Please provide both a question and an answer.")

        st.markdown("---")
        st.info(f"Total cards: **{len(st.session_state.deck)}**")
    # ---------- ADMIN PANEL (impersonation & user management) ----------
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
                new_name = st.text_input(f"Rename {uname}", value=uname, key=f"rename_{uid}")
                if st.button(f"Save username {uid}", key=f"btn_rename_{uid}"):
                    cur.execute("UPDATE users SET username=? WHERE id=?", (new_name, uid))
                    conn.commit()
                    admin_log(real_user_id, uid, f"Renamed user '{uname}' to '{new_name}'")
                    st.success("Username updated.")
                    st.experimental_rerun() if hasattr(st, "experimental_rerun") else st.rerun()

            # Change password
            with col2:
                new_pw = st.text_input(f"New password for {uname}", type="password", key=f"newpw_{uid}")
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
                                # Delete subjects
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
                                        f"DELETE FROM uploaded_files WHERE subject_id IN ({placeholders}) AND user_id=?",
                                        (*subj_ids, uid),
                                    )
                                    # Delete subjects
                                    cur.execute(
                                        f"DELETE FROM subjects WHERE id IN ({placeholders})",
                                        subj_ids,
                                    )
                                # Delete remaining uploaded_files for this user
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

    # ---------- Main layout ----------
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
                            normalize_text(c.question)
                            for c in deck
                            if c.subject_id == subject_id
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
                            admin_log(real_user_id, user_id, f"Generated {new_cards_count} cards from uploaded PDFs")

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
                            if c.subject_id == current_subject_id and c.source_pdf == fmeta["filename"]
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
                                admin_log(real_user_id, user_id, f"Generated {added} new cards from {fmeta['filename']}")
                            if hasattr(st, "experimental_rerun"):
                                st.experimental_rerun()
                            else:
                                st.rerun()
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
                                        admin_log(real_user_id, user_id, f"Deleted file {fmeta['filename']} and its cards")
                                    st.session_state.pop(confirm_key, None)
                                    if hasattr(st, "experimental_rerun"):
                                        st.experimental_rerun()
                                    else:
                                        st.rerun()

                            with c2:
                                if st.button("Cancel", key=f"cancel_{delete_key}"):
                                    st.session_state.pop(confirm_key, None)
                                    if hasattr(st, "experimental_rerun"):
                                        st.experimental_rerun()
                                    else:
                                        st.rerun()

    st.markdown("---")
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
                f'<div class="question-card"><div class="question-text">'
                f'<strong>Q:</strong> {card.question}'
                f'</div></div>',
                unsafe_allow_html=True,
            )
            st.caption(f"Type: {card.card_type}, Source: {card.source_pdf}, page {card.page}")

            if st.button("Show answer", key="show_srs_answer"):
                st.session_state.show_answer = True

            if st.session_state.show_answer:
                st.markdown(
                    f'<div class="question-card"><div class="question-text">'
                    f'<strong>A:</strong> {card.answer}'
                    f'</div></div>',
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
                            if hasattr(st, "experimental_rerun"):
                                st.experimental_rerun()
                            else:
                                st.rerun()

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
                    f'<div class="question-card">'
                    f'<div class="question-text"><strong>Q:</strong> {card.question}</div>'
                    f'</div>',
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
                    if hasattr(st, "experimental_rerun"):
                        st.experimental_rerun()
                    else:
                        st.rerun()

            with col_next:
                if st.button("Next ➡", disabled=(q_idx == len(quiz_items) - 1)):
                    st.session_state.quiz_index = min(len(quiz_items) - 1, q_idx + 1)
                    if hasattr(st, "experimental_rerun"):
                        st.experimental_rerun()
                    else:
                        st.rerun()

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
            import pandas as pd

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
                                    st.session_state.deck = [
                                        d for d in st.session_state.deck if d.id != c.id
                                    ]
                                    st.success(f"Card {c.id} deleted.")
                                    if real_user_id != user_id:
                                        admin_log(real_user_id, user_id, f"Deleted card {c.id}")
                                else:
                                    st.error("Could not delete card.")
                                st.session_state.pop(confirm_key, None)
                                if hasattr(st, "experimental_rerun"):
                                    st.experimental_rerun()
                                else:
                                    st.rerun()

                        with col_n:
                            if st.button("Cancel", key=f"cancel_{delete_key}"):
                                st.session_state.pop(confirm_key, None)
                                if hasattr(st, "experimental_rerun"):
                                    st.experimental_rerun()
                                else:
                                    st.rerun()


if __name__ == "__main__":
    main()