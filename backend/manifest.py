"""Canonical MultiCaRe manifest helpers shared by preparation, indexing and API serving."""

from __future__ import annotations

import copy
import os
from pathlib import Path

MANIFEST_SCHEMA_VERSION = "multicare-cxr/v1"
DATASET_ROOT = Path(os.getenv("DATASET_DIR", str(Path(__file__).resolve().parent.parent / "dataset"))).resolve()
MANIFEST_PATH = DATASET_ROOT / "manifest.json"
ASSET_ROOT = DATASET_ROOT / "assets"


def empty_profile(*, article_id: str, image_id: str, provenance: dict | None = None) -> dict:
    """The only profile shape stored and consumed by this application."""
    provenance = provenance or {}
    return {
        "profile_id": f"{article_id}:{image_id}", "case_id": article_id, "image_id": image_id,
        "patient": {"age_years": None, "sex": None, "immunocompromised": None, "weight_kg": None,
                    "comorbidities": [], "medications": [], "allergies": None},
        "presentation": {"chief_complaint": None, "symptom_duration": None, "hpi": None, "pmh": None},
        "study": {"modality": "CXR", "body_region": "chest", "view_position": None,
                  "radiology_region": None, "caption": None, "image_type": "radiology",
                  "image_subtype": "x_ray", "image_url": None, "storage_path": None},
        "assessment": {"diagnosis_primary": None, "suspected_primary": [], "differential": [],
                       "urgency": None, "infectious_concern": None, "icu_candidate": None},
        "findings": {"lungs": {"consolidation_present": None, "consolidation_locations": [],
                                  "consolidation_extent": None, "atelectasis_present": None,
                                  "atelectasis_locations": [], "edema_present": None, "edema_pattern": None},
                     "pleura": {"effusion_present": None, "effusion_side": None, "effusion_size": None,
                                "pneumothorax_present": None, "pneumothorax_side": None},
                     "cardiomediastinal": {"cardiomegaly": None, "mediastinal_widening": None},
                     "devices": {"lines_tubes_present": None, "device_list": []}},
        "summary": {"one_liner": None, "key_points": [], "red_flags": []},
        "outcome": {"success": None, "detail": None},
        "provenance": {"dataset_name": "MultiCaRe", "pmc_id": article_id,
                       "pmid": provenance.get("pmid"), "doi": provenance.get("doi"),
                       "article_title": provenance.get("title"), "journal": provenance.get("journal"),
                       "year": provenance.get("year"), "authors": provenance.get("authors", []),
                       "license": provenance.get("license"), "source_url": provenance.get("link")},
        "tags": {"ml_labels": [], "gt_labels": [], "keywords": provenance.get("keywords", []),
                 "mesh_terms": provenance.get("mesh_terms", [])}, "extra_fields": {},
    }


def merge_profile(base: dict, enrichment: dict | None) -> dict:
    """Accept only known profile fields, retaining source-grounded profile defaults."""
    result = copy.deepcopy(base)
    if not isinstance(enrichment, dict):
        return result
    for section, value in enrichment.items():
        if section not in result or not isinstance(value, dict) or not isinstance(result[section], dict):
            continue
        for key, field in value.items():
            if key in result[section] and field is not None:
                result[section][key] = field
    return normalize_profile(result)


def normalize_profile(profile: dict | None) -> dict:
    """Return a canonical profile with list fields safe for API/UI consumers.

    Local-model enrichment is deliberately best-effort, so an otherwise useful
    response can occasionally supply one item as a string instead of a JSON
    list.  The manifest schema promises lists for those fields.  Coercing them
    here protects both newly prepared records and already-indexed payloads
    without inventing any clinical information.
    """
    source = profile if isinstance(profile, dict) else {}
    provenance = source.get("provenance") if isinstance(source.get("provenance"), dict) else {}
    result = empty_profile(
        article_id=str(source.get("case_id") or provenance.get("pmc_id") or "unknown"),
        image_id=str(source.get("image_id") or "unknown"),
        provenance=provenance,
    )

    def apply(template: dict, values: dict) -> None:
        for key, default in template.items():
            if key not in values or values[key] is None:
                continue
            value = values[key]
            if isinstance(default, dict):
                if isinstance(value, dict):
                    apply(default, value)
            elif isinstance(default, list):
                # Keep a scalar extracted by the local model as one explicit
                # item rather than treating the characters of a string as items.
                template[key] = value if isinstance(value, list) else [value]
            else:
                template[key] = value

    apply(result, source)
    return result


def asset_path(asset_id: str) -> Path:
    if not asset_id or "/" in asset_id or "\\" in asset_id or asset_id in {".", ".."}:
        raise ValueError("Invalid dataset asset identifier")
    return ASSET_ROOT / asset_id
