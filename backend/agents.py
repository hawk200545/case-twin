"""Local-model helper for the optional hospital-page summary endpoint.

This deliberately avoids a hosted LLM provider; it is not involved in CXR
retrieval, but keeps the existing UI endpoint operational with the same local
gateway used everywhere else.
"""

from __future__ import annotations

import json
import re

import httpx

from local_ai import GEMMA_MODEL, query_local_model


def analyze_hospital_staff(url: str, diagnosis: str, hospital_name: str, location: str = "") -> list[dict]:
    try:
        response = httpx.get(url, follow_redirects=True, timeout=20.0)
        response.raise_for_status()
        # A bounded plain-text source prevents accidental huge prompt ingestion.
        page_text = re.sub(r"<[^>]+>", " ", response.text)
        page_text = re.sub(r"\s+", " ", page_text)[:12000]
    except Exception:
        page_text = ""
    prompt = f'''From the supplied hospital-page text, extract at most five named clinicians or relevant departments for
{diagnosis}. Do not invent people, contact data, or specialties. Return only a JSON array of objects with keys
name, specialty, context, url, phone. If the page has no supported results return [].

Hospital: {hospital_name}; location: {location}; source URL: {url}
Page text: {page_text or "No page text available."}'''
    try:
        text = query_local_model(prompt, model=GEMMA_MODEL, max_tokens=600)[0]["generated_text"]
        start, end = text.find("["), text.rfind("]")
        result = json.loads(text[start : end + 1]) if start >= 0 and end >= start else []
        return result[:5] if isinstance(result, list) else []
    except Exception:
        return []
