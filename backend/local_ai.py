"""Strict clients for the local OpenAI-compatible gateway and MedSigLIP service."""

import base64
import io
import math
import os
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from PIL import Image

# The preparation/index commands run from the repository root, while Compose
# injects the same file as environment variables. Loading this adjacent file
# makes both execution paths use one backend configuration source.
load_dotenv(Path(__file__).with_name(".env"))

GATEWAY_BASE_URL = os.getenv("LOCAL_GATEWAY_BASE_URL", os.getenv("LOCAL_LLM_BASE_URL", "")).rstrip("/")
if GATEWAY_BASE_URL and not GATEWAY_BASE_URL.endswith("/v1"):
    GATEWAY_BASE_URL = f"{GATEWAY_BASE_URL}/v1"
GATEWAY_API_KEY = os.getenv("LOCAL_GATEWAY_API_KEY", os.getenv("LOCAL_LLM_API_KEY", "")).strip()
MEDGEMMA_MODEL = os.getenv("MEDGEMMA_MODEL", os.getenv("LOCAL_MODEL_NAME", "medgemma")).strip()
GEMMA_MODEL = os.getenv("GEMMA_MODEL", os.getenv("LOCAL_GENERAL_MODEL_NAME", "gemma-4")).strip()
MEDSIGLIP_BASE_URL = os.getenv("MEDSIGLIP_BASE_URL", os.getenv("MEDSIGLIP_ENDPOINT", "")).rstrip("/")
MEDSIGLIP_API_KEY = os.getenv("MEDSIGLIP_API_KEY", "").strip()


def _image_b64(image: Image.Image) -> str:
    image = image.convert("RGB")
    image.thumbnail((768, 768))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _normalise(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if not math.isfinite(norm) or norm == 0:
        raise ValueError("MedSigLIP returned an invalid or zero vector")
    return [value / norm for value in vector]


def _gateway_headers() -> dict[str, str]:
    if not GATEWAY_BASE_URL:
        raise RuntimeError("LOCAL_GATEWAY_BASE_URL is not configured")
    if not GATEWAY_API_KEY:
        raise RuntimeError("LOCAL_GATEWAY_API_KEY is not configured")
    return {"Authorization": f"Bearer {GATEWAY_API_KEY}"}


def query_local_model(
    prompt: str, *, model: str = MEDGEMMA_MODEL, image: Image.Image | None = None,
    max_tokens: int = 300, stop_sequences: list[str] | None = None,
) -> list[dict[str, str]]:
    """Call the OpenAI-compatible chat API and preserve the legacy response shape."""
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    if image is not None:
        content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_image_b64(image)}"}})
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": max_tokens,
    }
    if stop_sequences:
        payload["stop"] = stop_sequences
    response = httpx.post(f"{GATEWAY_BASE_URL}/chat/completions", headers=_gateway_headers(), json=payload, timeout=120.0)
    response.raise_for_status()
    try:
        choice = response.json()["choices"][0]
        content_text = choice["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("Unexpected local gateway chat response") from exc
    if not isinstance(content_text, str) or not content_text.strip():
        finish_reason = choice.get("finish_reason") if isinstance(choice, dict) else None
        raise ValueError(
            f"Local gateway returned empty chat content for model {model!r} "
            f"(finish_reason={finish_reason!r})"
        )
    return [{"generated_text": content_text}]


def query_medgemma(image: Image.Image, prompt: str = "Describe this chest X-ray.", max_tokens: int = 200,
                   stop_sequences: list[str] | None = None) -> list[dict[str, str]]:
    return query_local_model(prompt, model=MEDGEMMA_MODEL, image=image, max_tokens=max_tokens, stop_sequences=stop_sequences)


def generate_embedding(image: Image.Image) -> list[float]:
    """Generate one non-empty, normalized image vector; never silently substitute one."""
    if not MEDSIGLIP_BASE_URL:
        raise RuntimeError("MEDSIGLIP_BASE_URL is not configured")
    if not MEDSIGLIP_API_KEY:
        raise RuntimeError("MEDSIGLIP_API_KEY is not configured")
    response = httpx.post(
        f"{MEDSIGLIP_BASE_URL}/v1/embed_image",
        headers={"Authorization": f"Bearer {MEDSIGLIP_API_KEY}"},
        json={"images_b64": [_image_b64(image)]}, timeout=120.0,
    )
    response.raise_for_status()
    payload: Any = response.json()
    vector: Any = payload
    if isinstance(payload, dict):
        vector = payload.get("embedding") or payload.get("embeddings") or payload.get("vectors")
    if isinstance(vector, list) and vector and isinstance(vector[0], list):
        vector = vector[0]
    if not isinstance(vector, list) or not vector:
        raise ValueError("Unexpected MedSigLIP embedding response")
    try:
        values = [float(value) for value in vector]
    except (TypeError, ValueError) as exc:
        raise ValueError("MedSigLIP returned a non-numeric vector") from exc
    if not all(math.isfinite(value) for value in values):
        raise ValueError("MedSigLIP returned non-finite vector values")
    return _normalise(values)
