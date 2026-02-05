# card_generation.py
import json
import re
from difflib import SequenceMatcher
from typing import List

import fitz  # PyMuPDF
import streamlit as st

import llm_client as llm
from models import QAItem
from db import insert_card


# =========================
# Helpers for deduplication
# =========================

def normalize_text(s: str) -> str:
    """Normalize text for deduplication: lowercase, trim, collapse internal spaces."""
    return re.sub(r"\s+", " ", s.strip().lower())


def is_similar_to_existing(
    norm_question: str,
    existing_norm_questions: List[str],
    threshold: float = 0.85,
) -> bool:
    """
    Check if norm_question is too similar to any existing normalized question.
    Uses difflib.SequenceMatcher for approximate similarity.
    threshold in [0, 1]; higher = stricter dedup.
    """
    for q in existing_norm_questions:
        if SequenceMatcher(None, norm_question, q).ratio() >= threshold:
            return True
    return False


# =========================
# LLM Client (OpenAI-compatible)
# =========================

SYSTEM_PROMPT = """
You are an assistant that creates study materials (flashcards and quiz questions)
from technical or educational text. Output strictly valid JSON. No explanations.

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
        existing_norm_questions.append(norm_q)

    return items


# =========================
# PDF handling & text chunking
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


def generate_cards_from_pdf_path(
    pdf_path: str,
    pdf_name: str,
    subject_id: int,
    max_new_cards: int,
) -> int:
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
        normalize_text(c.question) for c in deck if c.subject_id == subject_id
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