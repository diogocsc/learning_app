from __future__ import annotations

import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Callable, List, Optional

import fitz  # PyMuPDF

from models import QAItem
from db import insert_card, get_excluded_pages_map, load_all_cards
from rag_store import retrieve, add_documents
from .llm import ask_question, LLMError


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
""".strip()


def normalize_text(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def is_similar_to_existing(norm_question: str, existing_norm_questions: List[str], threshold: float = 0.85) -> bool:
    for q in existing_norm_questions:
        if SequenceMatcher(None, norm_question, q).ratio() >= threshold:
            return True
    return False


def is_metadata_question(question: str, answer: str) -> bool:
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


def extract_pages_from_pdf_bytes(file_bytes: bytes) -> List[dict]:
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    pages = []
    for i, page in enumerate(doc, start=1):
        pages.append({"page": i, "text": page.get_text()})
    doc.close()
    return pages


def chunk_page_text(page_text: str, max_chars: int = 1200) -> List[str]:
    paragraphs = [p.strip() for p in page_text.split("\n") if p.strip()]
    chunks: List[str] = []
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


def generate_cards_from_chunk(
    *,
    user_id: int,
    subject_id: int,
    chunk_text: str,
    page: int,
    source_pdf: str,
    starting_id: int,
    max_items_for_this_chunk: int,
    existing_norm_questions: List[str],
) -> List[QAItem]:
    retrieved_context = retrieve(user_id=user_id, subject_id=subject_id, query=chunk_text, k=5)
    context_text = "\n\n".join(retrieved_context)

    user_prompt = f"""
Context (retrieved from knowledge base):
\"\"\"{context_text}\"\"\"

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
""".strip()

    content = ask_question(SYSTEM_PROMPT + "\n\n" + user_prompt)
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
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
        if is_metadata_question(question, answer):
            continue

        norm_q = normalize_text(question)
        if is_similar_to_existing(norm_q, existing_norm_questions, threshold=0.85):
            continue

        if card_type == "multiple_choice":
            if not isinstance(options, list) or not all(isinstance(o, str) for o in options):
                continue
        else:
            options = None

        qa = QAItem(
            id=idx,
            card_type=card_type,
            question=str(question).strip(),
            answer=str(answer).strip(),
            source_pdf=source_pdf,
            page=page,
            subject_id=subject_id,
            options=options,
        )
        items.append(qa)
        idx += 1
        existing_norm_questions.append(norm_q)

    return items


def generate_cards_from_pdf_bytes(
    *,
    user_id: int,
    subject_id: int,
    pdf_name: str,
    file_bytes: bytes,
    file_id: int,
    max_cards: int,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
) -> int:
    """
    Stratified sampling across the whole PDF:
    - Build a list of valid pages (not excluded, not empty)
    - Select up to K pages uniformly across the document (K <= max_cards)
    - Allocate a per-page budget so questions are balanced across the PDF
    """
    max_cards = max(1, int(max_cards))

    deck = load_all_cards(user_id)
    current_id = max((c.id for c in deck), default=0) + 1
    new_cards_count = 0

    existing_norm_questions = [normalize_text(c.question) for c in deck if c.subject_id == subject_id]
    excluded_pages = set(get_excluded_pages_map(file_id))

    pages = extract_pages_from_pdf_bytes(file_bytes)

    # Build valid pages with pre-chunking for deterministic sampling + progress.
    valid_pages: list[dict] = []
    for page_info in pages:
        page_number = int(page_info["page"])
        if page_number in excluded_pages:
            continue
        text = str(page_info["text"] or "")
        if not text.strip():
            continue
        chunks = chunk_page_text(text)
        if not chunks:
            continue
        valid_pages.append(
            {
                "page": page_number,
                "text_len": len(text),
                "chunks": chunks,
            }
        )

    if not valid_pages:
        if on_progress:
            on_progress(1, 1, "Nothing to generate (no valid pages).")
        return 0

    # ---- Stratified sampling: pick K pages uniformly across the doc ----
    n = len(valid_pages)
    k = min(n, max_cards)  # at least 1 question per sampled page
    if k == 1:
        sampled = [valid_pages[n // 2]]
    else:
        # Uniform positions across [0..n-1]
        indices = []
        for i in range(k):
            pos = round(i * (n - 1) / (k - 1))
            indices.append(int(pos))
        # Ensure uniqueness while preserving order
        seen = set()
        uniq = []
        for idx in indices:
            if idx not in seen:
                seen.add(idx)
                uniq.append(idx)
        sampled = [valid_pages[i] for i in uniq]

    # ---- Budget allocation across sampled pages ----
    k_eff = len(sampled)
    base = max_cards // k_eff
    remainder = max_cards % k_eff
    # Give remainder to longer pages first (small bias toward content-heavy pages)
    order_by_len = sorted(range(k_eff), key=lambda i: sampled[i]["text_len"], reverse=True)
    bonus_set = set(order_by_len[:remainder])

    per_page_budget = [base + (1 if i in bonus_set else 0) for i in range(k_eff)]

    # ---- Progress units: chunks processed + 1 indexing step ----
    total_chunks = sum(len(p["chunks"]) for p in sampled)
    total_units = max(total_chunks + 1, 1)
    completed_units = 0
    if on_progress:
        on_progress(0, total_units, f"Preparing balanced generation across {k_eff} pages…")

    rag_chunks: list[str] = []

    # ---- Generation: iterate sampled pages, consume per-page budgets ----
    for page_idx, page in enumerate(sampled):
        if new_cards_count >= max_cards:
            break
        page_number = int(page["page"])
        page_budget = per_page_budget[page_idx]
        remaining_page = page_budget

        chunks = page["chunks"]
        rag_chunks.extend(chunks)

        for chunk in chunks:
            if new_cards_count >= max_cards or remaining_page <= 0:
                break

            if on_progress:
                on_progress(min(completed_units, total_units - 1), total_units, f"Generating (page {page_number})…")

            per_chunk_limit = min(7, max_cards - new_cards_count, remaining_page)
            new_items = generate_cards_from_chunk(
                user_id=user_id,
                subject_id=subject_id,
                chunk_text=chunk,
                page=page_number,
                source_pdf=pdf_name,
                starting_id=current_id,
                max_items_for_this_chunk=per_chunk_limit,
                existing_norm_questions=existing_norm_questions,
            )

            for card in new_items:
                insert_card(card)

            added_now = len(new_items)
            new_cards_count += added_now
            current_id += added_now
            remaining_page -= added_now

            completed_units += 1
            if on_progress:
                on_progress(min(completed_units, total_units - 1), total_units, f"Generating (page {page_number})…")

    # If we still have budget left (LLM returned fewer than asked), do a second pass
    # over sampled pages in the same order, using remaining global budget.
    remaining_global = max_cards - new_cards_count
    if remaining_global > 0:
        for page in sampled:
            if remaining_global <= 0:
                break
            page_number = int(page["page"])
            for chunk in page["chunks"]:
                if remaining_global <= 0:
                    break
                if on_progress:
                    on_progress(min(completed_units, total_units - 1), total_units, f"Filling gaps (page {page_number})…")
                per_chunk_limit = min(7, remaining_global)
                new_items = generate_cards_from_chunk(
                    user_id=user_id,
                    subject_id=subject_id,
                    chunk_text=chunk,
                    page=page_number,
                    source_pdf=pdf_name,
                    starting_id=current_id,
                    max_items_for_this_chunk=per_chunk_limit,
                    existing_norm_questions=existing_norm_questions,
                )
                for card in new_items:
                    insert_card(card)
                added_now = len(new_items)
                new_cards_count += added_now
                current_id += added_now
                remaining_global -= added_now

                completed_units += 1
                if on_progress:
                    on_progress(min(completed_units, total_units - 1), total_units, f"Filling gaps (page {page_number})…")

    # Index chunks (only from sampled pages)
    if rag_chunks:
        if on_progress:
            on_progress(min(completed_units, total_units - 1), total_units, "Indexing knowledge base…")
        add_documents(user_id=user_id, subject_id=subject_id, docs=rag_chunks)
        completed_units = total_units
        if on_progress:
            on_progress(completed_units, total_units, "Finalizing…")

    return new_cards_count

