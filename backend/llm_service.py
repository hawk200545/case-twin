"""
Text-only LLM service for the self-hosted OpenAI-compatible LiteLLM gateway.

Fully local — no cloud LLM fallback exists. Serves the "general intelligence"
tasks (hospital enrichment, fallback hospital generation). All clinical
vision tasks flow through embedding_service.query_medgemma, which targets
the same gateway with model=medgemma.

Configuration (backend/.env):
  LLM_API_TYPE=openai
  LOCAL_LLM_BASE_URL=http://10.129.6.150:8000      (trailing /v1 optional)
  LOCAL_GENERAL_MODEL_NAME=gemma-4                 (general chat + vision)
  LOCAL_LLM_API_KEY=<gateway key>                  (LiteLLM gateway Bearer key)

Note: gemma-4 emits reasoning tokens before the answer — keep max_tokens
generous (>= 1000) so the final answer is not truncated.
"""

import os
import threading

import httpx
from dotenv import load_dotenv

load_dotenv()

LLM_API_TYPE: str = os.getenv("LLM_API_TYPE", "ollama").strip().lower()
LOCAL_LLM_API_KEY: str = os.getenv("LOCAL_LLM_API_KEY", "").strip()
LOCAL_GENERAL_MODEL_NAME: str = os.getenv("LOCAL_GENERAL_MODEL_NAME", "gemma-4").strip() or "gemma-4"

# Accept base URLs with or without a trailing /v1 (and trailing slash).
_base = os.getenv("LOCAL_LLM_BASE_URL", "").strip().rstrip("/")
if _base.endswith("/v1"):
    _base = _base[:-3].rstrip("/")
LOCAL_LLM_BASE_URL: str = _base


def is_configured() -> bool:
    return bool(LOCAL_LLM_BASE_URL) and LLM_API_TYPE == "openai"


_http_client: httpx.Client | None = None
_client_lock = threading.Lock()


def _get_client() -> httpx.Client:
    global _http_client
    if _http_client is not None:
        return _http_client
    with _client_lock:
        if _http_client is not None:
            return _http_client
        _http_client = httpx.Client(timeout=180.0)
        return _http_client


def query_general_llm(prompt: str, max_tokens: int = 2048, temperature: float = 0.4) -> str:
    """
    Send a text-only prompt to the local OpenAI-compatible chat completions
    endpoint and return the assistant's reply text.

    Raises on any failure so callers can degrade gracefully (no cloud fallback).
    """
    if not is_configured():
        raise RuntimeError(
            "Local general LLM not configured — set LLM_API_TYPE=openai and LOCAL_LLM_BASE_URL in .env"
        )

    payload = {
        "model": LOCAL_GENERAL_MODEL_NAME,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    headers = {"Content-Type": "application/json"}
    if LOCAL_LLM_API_KEY:
        headers["Authorization"] = f"Bearer {LOCAL_LLM_API_KEY}"

    response = _get_client().post(
        f"{LOCAL_LLM_BASE_URL}/v1/chat/completions",
        json=payload,
        headers=headers,
    )
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"]
