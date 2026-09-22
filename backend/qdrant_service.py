"""Qdrant retrieval over the canonical MultiCaRe CXR manifest payload."""

from __future__ import annotations

import os
from qdrant_client import QdrantClient

from manifest import normalize_profile

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY") or None
COLLECTION_NAME = os.getenv("COLLECTION_NAME", "multicare_cxr")
_client: QdrantClient | None = None


def _get_client() -> QdrantClient:
    global _client
    if _client is None:
        _client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)
    return _client


def _profile_text(profile: dict) -> str:
    values = [profile.get("assessment", {}).get("diagnosis_primary"), profile.get("presentation", {}).get("chief_complaint"), profile.get("presentation", {}).get("hpi"), profile.get("summary", {}).get("one_liner")]
    return " ".join(str(value) for value in values if value).lower()


def _context_score(candidate: dict, query: dict | None) -> float:
    """Small deterministic profile adjustment; both sides share canonical shape."""
    if not query:
        return 0.0
    score = 0.0
    candidate_patient, query_patient = candidate.get("patient", {}), query.get("patient", {})
    if candidate_patient.get("sex") and query_patient.get("sex") and str(candidate_patient["sex"])[0].lower() == str(query_patient["sex"])[0].lower():
        score += 0.1
    try:
        if abs(float(candidate_patient.get("age_years")) - float(query_patient.get("age_years"))) <= 10:
            score += 0.1
    except (TypeError, ValueError):
        pass
    candidate_text, query_text = _profile_text(candidate), _profile_text(query)
    tokens = {token for token in query_text.split() if len(token) > 3}
    return score + min(0.25, 0.025 * sum(token in candidate_text for token in tokens))


def search_similar(embedding: list[float], profile_data: dict | None = None, limit: int = 5) -> list[dict]:
    if not embedding:
        raise ValueError("Cannot search with an empty vector")
    client = _get_client()
    retrieve_limit = min(100, max(limit, 30 if profile_data else limit))
    results = client.query_points(collection_name=COLLECTION_NAME, query=embedding, limit=retrieve_limit).points
    maximum = max((result.score for result in results), default=1.0) or 1.0
    candidates = []
    for result in results:
        payload = result.payload or {}
        profile = normalize_profile(payload.get("profile"))
        context = _context_score(profile, profile_data)
        fused = 0.7 * (result.score / maximum) + 0.3 * context if profile_data else result.score / maximum
        candidates.append((fused, result.score, result.id, payload))
    candidates.sort(key=lambda item: item[0], reverse=True)
    matches = []
    for _, visual_score, point_id, payload in candidates[:limit]:
        profile = normalize_profile(payload.get("profile"))
        assessment, summary, provenance, study = profile.get("assessment", {}), profile.get("summary", {}), profile.get("provenance", {}), profile.get("study", {})
        primary = assessment.get("diagnosis_primary") or summary.get("one_liner") or study.get("caption") or "Historical CXR case"
        matches.append({
            "id": str(point_id), "score": round(visual_score * 100), "diagnosis": str(primary)[:120],
            "summary": summary.get("one_liner") or study.get("caption") or "No structured summary available.",
            "facility": "MultiCaRe", "outcome": profile.get("outcome", {}).get("detail") or "Historical case report",
            "outcomeVariant": "success" if visual_score >= .8 else "warning" if visual_score >= .6 else "neutral",
            "asset_id": payload.get("primary_asset_id"), "related_asset_ids": payload.get("related_asset_ids", []),
            "age": profile.get("patient", {}).get("age_years"), "gender": profile.get("patient", {}).get("sex"),
            "pmc_id": provenance.get("pmc_id"), "article_title": provenance.get("article_title"),
            "journal": provenance.get("journal"), "year": provenance.get("year"), "radiology_view": study.get("view_position"),
            "case_text": profile.get("presentation", {}).get("hpi") or payload.get("raw_narrative", ""),
            # Preserve the existing UI contract while deriving every field from the
            # one canonical profile (never the former schema variants).
            "raw_payload": {**profile, "primary_asset_id": payload.get("primary_asset_id"),
                            "related_images": payload.get("related_images", [])
                            if isinstance(payload.get("related_images"), list) else []},
        })
    return matches
