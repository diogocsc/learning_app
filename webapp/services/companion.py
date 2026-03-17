from __future__ import annotations

from typing import List, Dict

from rag_store import retrieve
from .llm import ask_question


SYSTEM_PROMPT = """
You are an AI study companion inside a learning app.
Be concise, friendly, and actionable.

You must:
- Suggest exercises that match the user's subject/material.
- Prefer asking 1 clarifying question if the request is ambiguous.
- When you propose exercises, include:
  - 3 to 7 items
  - a mix of difficulty (easy/medium/hard)
  - the expected answer format (e.g., short answer, multiple choice, explain, derive)
- If you rely on provided context, incorporate it; if context is empty, say so and give general exercises.

Never mention internal implementation details (RAG, vector DB, embeddings).
""".strip()


def build_prompt(*, user_message: str, context_chunks: List[str], recent_messages: List[Dict[str, str]]) -> str:
    context_text = "\n\n".join(context_chunks[:8]).strip()
    history_lines: list[str] = []
    for m in recent_messages[-8:]:
        role = m.get("role", "")
        content = (m.get("content") or "").strip()
        if not content:
            continue
        if role == "user":
            history_lines.append(f"User: {content}")
        elif role == "assistant":
            history_lines.append(f"Assistant: {content}")

    history = "\n".join(history_lines).strip()

    return f"""
{SYSTEM_PROMPT}

Context from the user's uploaded materials (may be empty):
\"\"\"{context_text}\"\"\"

Recent conversation:
{history if history else "(none)"}

User message:
{user_message}

Respond as the assistant.
""".strip()


def companion_reply(*, user_id: int, subject_id: int, user_message: str, recent_messages: List[Dict[str, str]]) -> str:
    context_chunks = retrieve(user_id=user_id, subject_id=subject_id, query=user_message, k=6)
    prompt = build_prompt(user_message=user_message, context_chunks=context_chunks, recent_messages=recent_messages)
    return ask_question(prompt, timeout_s=180).strip()

