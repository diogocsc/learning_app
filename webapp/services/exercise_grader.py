from __future__ import annotations

import json
from typing import Any, Dict

from .llm import ask_question


def _safe_json_parse(s: str) -> Dict[str, Any]:
    s = (s or "").strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        # Best-effort extraction of the first JSON object in the output.
        start = s.find("{")
        end = s.rfind("}")
        if start >= 0 and end > start:
            return json.loads(s[start : end + 1])
        raise


def grade_exercise_answer(
    *,
    card_type: str,
    question: str,
    expected_answer: str,
    user_answer: str,
    timeout_s: int = 180,
) -> Dict[str, Any]:
    """
    Uses the LLM to grade open-ended / coding-like exercises.

    Returns:
      { "is_correct": bool, "quality": int(0-5), "feedback": str }
    """
    # Note: we keep the prompt strict ("JSON only") and small, but include enough context
    # for consistent scoring. The model is not expected to run code; it judges by reasoning.
    prompt = f"""
You are a strict grader for an AI learning app.
Grade the student's response against the expected answer.
Return ONLY strictly valid JSON (no markdown, no commentary).

JSON schema:
{{
  "is_correct": true|false,
  "quality": 0|1|2|3|4|5,
  "feedback": "short helpful feedback"
}}

Card type: {card_type}

Question:
\"\"\"{question}\"\"\"

Expected answer / reference solution:
\"\"\"{expected_answer}\"\"\"

Student response:
\"\"\"{user_answer}\"\"\"

Scoring rules:
- quality 5: complete and correct, matches the expected solution strongly.
- quality 4: mostly correct with minor gaps.
- quality 3: partially correct or missing key details, but shows understanding.
- quality 2: mostly incorrect or unsupported.
- quality 1: very wrong.
- quality 0: empty or nonsense.

Set is_correct to true only when the response satisfies the expected answer at quality >= 3.
Feedback must be concise (1-3 sentences).
""".strip()

    raw = ask_question(prompt, timeout_s=timeout_s)
    parsed = _safe_json_parse(raw)

    quality = int(parsed.get("quality", 0))
    quality = max(0, min(5, quality))
    is_correct = bool(parsed.get("is_correct", quality >= 3))
    feedback = str(parsed.get("feedback", "") or "").strip()

    return {"is_correct": is_correct, "quality": quality, "feedback": feedback}

