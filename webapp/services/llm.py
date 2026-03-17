from __future__ import annotations

import json
import os
from typing import Optional

import requests


class LLMError(RuntimeError):
    pass


def ask_question(prompt: str, *, model: str | None = None, timeout_s: int = 120) -> str:
    """
    Stream the Ollama Cloud generate API and return concatenated `response` text.
    Reads credentials from environment:
      - OLLAMA_API_KEY (required)
      - OLLAMA_BASE_URL (optional; default https://ollama.com)
    """
    api_key = os.getenv("OLLAMA_API_KEY")
    if not api_key:
        raise LLMError("Missing OLLAMA_API_KEY environment variable")

    base_url = os.getenv("OLLAMA_BASE_URL", "https://ollama.com").rstrip("/")
    url = f"{base_url}/api/generate"

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    if not model:
        model = os.getenv("OLLAMA_MODEL", "gpt-oss:120b")
    payload = {"model": model, "prompt": prompt}

    full_response = ""
    try:
        with requests.post(url, headers=headers, json=payload, stream=True, timeout=timeout_s) as r:
            if r.status_code >= 400:
                raise LLMError(f"LLM request failed: HTTP {r.status_code}")
            for line in r.iter_lines():
                if not line:
                    continue
                try:
                    data = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    continue
                chunk = data.get("response")
                if isinstance(chunk, str):
                    full_response += chunk
    except requests.RequestException as e:
        raise LLMError(f"LLM request error: {e}") from e

    return full_response

