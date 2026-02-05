# auth.py
import random
from datetime import datetime

import bcrypt
import streamlit as st

from db import create_user, get_user_by_username
from config import MAX_REG_ATTEMPTS, REG_RATE_WINDOW, CUSTOM_CSS


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