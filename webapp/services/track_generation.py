from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from db import (
    get_connection,
    create_track,
    insert_lesson,
    get_uploaded_files,
    get_excluded_pages_map,
    load_all_cards,
)

from config import UPLOAD_DIR
from rag_store import add_documents, clear_index, retrieve

from .llm import ask_question
from .generation import chunk_page_text, extract_pages_from_pdf_bytes, generate_cards_from_chunk, normalize_text as _gen_normalize_text


def _safe_json_parse(s: str) -> Dict[str, Any]:
    s = (s or "").strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        start = s.find("{")
        end = s.rfind("}")
        if start >= 0 and end > start:
            return json.loads(s[start : end + 1])
        raise


def generate_track_from_subject_pdfs(
    *,
    user_id: int,
    subject_id: int,
    title: str,
    num_lessons: int,
    cards_per_lesson: int,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
) -> int:
    """
    Builds a curriculum track from ALL uploaded PDFs under `user_id + subject_id`.
    Generates gated lessons and tags resulting cards with (track_id, lesson_id).
    """
    # Resolve subject name (for prompt context).
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT name FROM subjects WHERE id=? AND user_id=?", (subject_id, user_id))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise RuntimeError("Subject not found.")
    subject_name = str(row[0])

    uploaded_files = get_uploaded_files(user_id, subject_id)
    if not uploaded_files:
        raise RuntimeError("No uploaded PDFs found for this subject.")

    total_units = 2 + num_lessons
    unit = 0

    # 1) Extract+index all chunks for deterministic lesson selection & RAG context.
    if on_progress:
        on_progress(unit, total_units, f"Indexing uploaded PDFs for '{subject_name}'…")
    unit += 1

    all_docs: List[str] = []
    # Map chunk text => list of (source_pdf, page). Enables metadata lookup after RAG retrieval.
    text_to_meta: Dict[str, List[Tuple[str, int]]] = defaultdict(list)
    # If the same chunk text appears multiple times, rotate which (pdf,page) we attribute it to.
    text_to_meta_cursor: Dict[str, int] = defaultdict(int)

    # Extract all chunks
    for f in uploaded_files:
        file_id = int(f["id"])
        stored_path = Path(f["stored_path"])
        if not stored_path.exists():
            continue

        excluded_pages = set(get_excluded_pages_map(file_id))
        pdf_name = f["filename"]

        file_bytes = stored_path.read_bytes()
        pages = extract_pages_from_pdf_bytes(file_bytes)
        for page_info in pages:
            page_number = int(page_info["page"])
            if page_number in excluded_pages:
                continue
            text = str(page_info.get("text") or "")
            if not text.strip():
                continue
            chunks = chunk_page_text(text)
            for ch in chunks:
                ch = str(ch).strip()
                if not ch:
                    continue
                all_docs.append(ch)
                text_to_meta[ch].append((pdf_name, page_number))

    if not all_docs:
        raise RuntimeError("No indexable text found in uploaded PDFs.")

    # Rebuild FAISS index for this subject so RAG retrieval returns only our current chunk set.
    clear_index(user_id, subject_id)
    add_documents(user_id=user_id, subject_id=subject_id, docs=all_docs)

    # 2) Ask LLM for a curriculum outline (lessons + anchors).
    context_chunks = retrieve(user_id=user_id, subject_id=subject_id, query=title or subject_name, k=8)
    context_text = "\n\n".join([c if isinstance(c, str) else str(c) for c in context_chunks])

    if on_progress:
        on_progress(unit, total_units, "Designing curriculum with LLM…")
    unit += 1

    # Auto-naming: when a non-empty title is provided, ask the LLM to use it verbatim.
    title_constraint = (
        f"- If `track_title` is requested, set it exactly to \"{title}\" (verbatim) when it is non-empty.\n"
        if title
        else "- You may choose a track title.\n"
    )

    outline_prompt = f"""
You are designing a learning track curriculum.
Use the user's uploaded PDF material as the grounding source.

Return ONLY strictly valid JSON.

Input context (snippets from the PDFs):
\"\"\"{context_text[:6000]}\"\"\"

We need exactly {num_lessons} lessons.
Each lesson must include:
- title (short)
- objectives (3-5 bullet-like short strings)
- anchor_queries (3-5 search queries that will retrieve relevant PDF chunks)
- brief (1 short paragraph summarizing what the student should learn in this lesson)

JSON schema:
{{
  "track_title": string,
  "lessons": [
    {{
      "title": string,
      "objectives": [string, ...],
      "anchor_queries": [string, ...],
      "brief": string,
      "bite_markdown": string
    }},
    ...
  ]
}}

Constraints:
- Lessons must be ordered from fundamentals to advanced.
- Anchor queries should be specific (key concepts/skills), not generic.
- Keep everything in the same language as the PDF snippets above.
{title_constraint}
""".strip()

    # LLM output can be imperfect (wrong count / malformed JSON), so retry a few times.
    last_outline_error: Optional[str] = None
    data: Dict[str, Any] = {}
    for attempt in range(3):
        try:
            raw = ask_question(outline_prompt, timeout_s=180)
            data = _safe_json_parse(raw)
            lessons = data.get("lessons") or []
            if not isinstance(lessons, list) or len(lessons) != int(num_lessons):
                last_outline_error = f"Expected {num_lessons} lessons, got {len(lessons) if isinstance(lessons, list) else 'non-list'}."
                continue
            break
        except Exception as e:
            last_outline_error = str(e)
            continue

    lessons = data.get("lessons") or []
    if not isinstance(lessons, list) or len(lessons) != int(num_lessons):
        raise RuntimeError(f"LLM outline generation failed. {last_outline_error or ''}".strip())

    track_title = str(data.get("track_title") or title or subject_name)
    track_id = create_track(
        user_id=user_id,
        subject_id=subject_id,
        title=track_title,
        num_lessons=int(num_lessons),
        cards_per_lesson=int(cards_per_lesson),
    )

    # Insert lessons
    lesson_rows: List[dict] = []
    for i, l in enumerate(lessons):
        lesson_title = str(l.get("title") or f"Lesson {i + 1}")
        objectives = l.get("objectives") or []
        if not isinstance(objectives, list):
            objectives = [str(objectives)]
        anchor_queries = l.get("anchor_queries") or []
        if not isinstance(anchor_queries, list):
            anchor_queries = [str(anchor_queries)]
        brief = l.get("brief")
        if brief is not None:
            brief = str(brief)

        bite_markdown = l.get("bite_markdown")
        if bite_markdown is not None and str(bite_markdown).strip():
            bite_markdown = str(bite_markdown).strip()
        else:
            bite_markdown = (str(brief).strip() if brief else "").strip()

        lesson_id = insert_lesson(
            track_id=track_id,
            lesson_index=i,
            title=lesson_title,
            objectives=[str(x) for x in objectives],
            anchor_queries=[str(x) for x in anchor_queries],
            brief=brief,
            bite_markdown=bite_markdown,
        )
        lesson_rows.append({"lesson_id": lesson_id, **l})

    # Index lesson briefs as additional LLM-generated context sources.
    brief_docs = [str(l.get("brief") or "").strip() for l in lessons]
    brief_docs = [b for b in brief_docs if b]
    if brief_docs:
        add_documents(user_id=user_id, subject_id=subject_id, docs=brief_docs)

    # 3) Generate cards per lesson.
    if on_progress:
        on_progress(unit, total_units, "Generating lesson cards…")
    # We'll spend ~1 unit per lesson generation.

    deck = load_all_cards(user_id)
    current_id = max((c.id for c in deck), default=0) + 1
    existing_norm_questions = [_gen_normalize_text(c.question) for c in deck if c.subject_id == subject_id]

    for lesson_idx, lr in enumerate(lesson_rows):
        if on_progress:
            on_progress(unit + lesson_idx, total_units, f"Generating cards for lesson {lesson_idx + 1}/{num_lessons}…")

        lesson_id = int(lr["lesson_id"])
        anchor_queries = lr.get("anchor_queries") or []
        if not isinstance(anchor_queries, list):
            anchor_queries = [str(anchor_queries)]

        lesson_target = int(cards_per_lesson)
        remaining = lesson_target
        lesson_cards_generated = 0
        llm_failures = 0

        # Candidate chunk selection from RAG (pdf chunks). We convert returned chunk texts to (source_pdf, page).
        # We also iterate anchor queries to get better coverage across the lesson topic.
        candidate_chunks: List[Tuple[str, str, int]] = []
        used_chunk_keys: set[str] = set()
        # Each candidate is (chunk_text, source_pdf, page)

        try:
            for anchor in anchor_queries:
                if remaining <= 0:
                    break
                # Pull more than needed; we'll filter by those that exist in our chunk metadata map.
                texts = retrieve(user_id=user_id, subject_id=subject_id, query=str(anchor), k=max(8, lesson_target))
                for t in texts:
                    if not isinstance(t, str):
                        t = str(t)
                    meta_list = text_to_meta.get(t) or []
                    if not meta_list:
                        continue  # likely an LLM brief doc without page/source metadata
                    cursor = int(text_to_meta_cursor[t])
                    pdf_name, page_number = meta_list[cursor % len(meta_list)]
                    text_to_meta_cursor[t] = cursor + 1
                    # Use a key to avoid repeating the same (text, pdf, page) repeatedly.
                    key = f"{pdf_name}:{page_number}:{hash(t)}"
                    if key in used_chunk_keys:
                        continue
                    used_chunk_keys.add(key)
                    candidate_chunks.append((t, pdf_name, page_number))
        except Exception as e:
            raise RuntimeError(
                f"Track generation failed while selecting chunks for lesson {lesson_idx + 1}/{num_lessons}: {e}"
            ) from e

        # Generate cards from candidate chunks until we hit the lesson budget.
        # Candidate_chunks ordering already follows retrieval relevance.
        try:
            for chunk_text, pdf_name, page_number in candidate_chunks:
                if remaining <= 0:
                    break

                per_chunk_limit = min(7, remaining)
                try:
                    new_items = generate_cards_from_chunk(
                        user_id=user_id,
                        subject_id=subject_id,
                        chunk_text=chunk_text,
                        page=page_number,
                        source_pdf=pdf_name,
                        starting_id=current_id,
                        max_items_for_this_chunk=per_chunk_limit,
                        existing_norm_questions=existing_norm_questions,
                    )
                except Exception as e:
                    llm_failures += 1
                    if on_progress:
                        on_progress(
                            unit + lesson_idx,
                            total_units,
                            f"LLM error (lesson {lesson_idx + 1}/{num_lessons}); retrying chunks…",
                        )
                    # If we keep failing repeatedly, abort with context.
                    if llm_failures >= 3:
                        raise RuntimeError(
                            f"Track generation LLM failed repeatedly in lesson {lesson_idx + 1}/{num_lessons}: {e}"
                        ) from e
                    continue

                # Persist each card with track/lesson tags.
                from db import insert_card as _insert_card  # local import to avoid circulars during Flask reload

                for qa in new_items:
                    _insert_card(qa, track_id=track_id, lesson_id=lesson_id)
                added_now = len(new_items)
                current_id += added_now
                remaining -= added_now
                lesson_cards_generated += added_now
        except Exception as e:
            raise RuntimeError(
                f"Track generation failed while generating cards for lesson {lesson_idx + 1}/{num_lessons}: {e}"
            ) from e

        # If the LLM didn't return enough cards, we still proceed to next lesson.
        if remaining > 0:
            # Second pass: retrieve more candidate chunks and try again to fill the lesson budget.
            try:
                for anchor in anchor_queries:
                    if remaining <= 0:
                        break
                    texts = retrieve(
                        user_id=user_id,
                        subject_id=subject_id,
                        query=str(anchor),
                        k=max(16, lesson_target * 2),
                    )
                    for t in texts:
                        if remaining <= 0:
                            break
                        if not isinstance(t, str):
                            t = str(t)
                        meta_list = text_to_meta.get(t) or []
                        if not meta_list:
                            continue

                        cursor = int(text_to_meta_cursor[t])
                        pdf_name, page_number = meta_list[cursor % len(meta_list)]
                        text_to_meta_cursor[t] = cursor + 1

                        key = f"{pdf_name}:{page_number}:{hash(t)}"
                        if key in used_chunk_keys:
                            continue
                        used_chunk_keys.add(key)

                        per_chunk_limit = min(7, remaining)
                        try:
                            new_items = generate_cards_from_chunk(
                                user_id=user_id,
                                subject_id=subject_id,
                                chunk_text=t,
                                page=page_number,
                                source_pdf=pdf_name,
                                starting_id=current_id,
                                max_items_for_this_chunk=per_chunk_limit,
                                existing_norm_questions=existing_norm_questions,
                            )
                        except Exception as e:
                            llm_failures += 1
                            if on_progress:
                                on_progress(
                                    unit + lesson_idx,
                                    total_units,
                                    f"LLM error in second-pass (lesson {lesson_idx + 1}/{num_lessons}); skipping…",
                                )
                            if llm_failures >= 3:
                                raise RuntimeError(
                                    f"Track generation LLM failed repeatedly in lesson {lesson_idx + 1}/{num_lessons} (second-pass): {e}"
                                ) from e
                            continue
                        from db import insert_card as _insert_card  # local import to avoid circulars during Flask reload

                        for qa in new_items:
                            _insert_card(qa, track_id=track_id, lesson_id=lesson_id)

                        added_now = len(new_items)
                        current_id += added_now
                        remaining -= added_now
                        lesson_cards_generated += added_now
            except Exception as e:
                raise RuntimeError(
                    f"Track generation failed in second-pass for lesson {lesson_idx + 1}/{num_lessons}: {e}"
                ) from e


    if on_progress:
        on_progress(total_units, total_units, "Done.")

    return track_id

