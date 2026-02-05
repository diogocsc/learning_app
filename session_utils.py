# session_utils.py
from typing import List

import streamlit as st

from models import QAItem, CardType
from db import load_all_cards, insert_card


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
