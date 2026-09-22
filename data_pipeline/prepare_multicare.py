"""Create the reproducible, derived MultiCaRe CXR dataset.

The source tree is read-only.  This command writes only ``dataset/``:
assets (content-addressed image copies), manifest.json, and enrichment-cache.
It can safely be re-run after an interruption; cached article enrichments and
verified asset copies are reused.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
import re
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
from local_ai import GEMMA_MODEL, query_local_model  # noqa: E402
from manifest import MANIFEST_SCHEMA_VERSION, empty_profile, merge_profile  # noqa: E402

SOURCE_DEFAULT = ROOT / "medical_datasets" / "whole_multicare_dataset"
DATASET_DEFAULT = ROOT / "dataset"
CXR_PATTERN = re.compile(r"chest\s*x[\s-]?ray|\bcxr\b", re.IGNORECASE)


def jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return None if pd.isna(value) else value
    if hasattr(value, "tolist"):
        return jsonable(value.tolist())
    if hasattr(value, "item"):
        return jsonable(value.item())
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)) or hasattr(value, "tolist"):
        try:
            return [jsonable(item) for item in list(value)]
        except TypeError:
            return str(value)
    return str(value)


def as_list(value: Any) -> list:
    value = jsonable(value)
    return value if isinstance(value, list) else []


def source_image_path(source: Path, filename: str) -> Path:
    # MultiCaRe's documented nesting is PMC1/PMC10/<filename>.
    filename = Path(str(filename)).name
    return source / filename[:4] / filename[:5] / filename


def article_id(value: Any) -> str:
    match = re.search(r"PMC\d+", str(value))
    return match.group(0) if match else ""


def source_checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def copy_asset(source_file: Path, assets_dir: Path) -> tuple[str, int, str]:
    checksum = source_checksum(source_file)
    suffix = source_file.suffix.lower()
    asset_id = f"{checksum}{suffix}" if suffix else checksum
    destination = assets_dir / asset_id
    if destination.exists():
        if source_checksum(destination) != checksum:
            raise RuntimeError(f"Existing derived asset failed integrity check: {destination}")
    else:
        # A distinct temporary filename makes concurrent workers safe even
        # when two source rows reference the same content-addressed asset.
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.part")
        try:
            shutil.copy2(source_file, temporary)
            if source_checksum(temporary) != checksum:
                raise RuntimeError(f"Copy integrity check failed: {source_file}")
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    return asset_id, source_file.stat().st_size, checksum


def parse_model_json(text: str) -> dict:
    candidate = text.strip()
    candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.IGNORECASE)
    decoder = json.JSONDecoder()
    # Gateways may prepend a short explanation or reasoning. Try every object
    # boundary rather than treating the first and last braces as one document.
    for match in re.finditer(r"\{", candidate):
        try:
            parsed, _ = decoder.raw_decode(candidate[match.start() :])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("Local gateway did not return a valid JSON object")


def enrich_article(article: str, abstract: str, narrative: str, cache_dir: Path) -> tuple[dict, str]:
    cache_file = cache_dir / f"{article}.json"
    source_hash = hashlib.sha256(f"{abstract}\n{narrative}".encode()).hexdigest()
    if cache_file.exists():
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        if (cached.get("source_hash") == source_hash and isinstance(cached.get("profile"), dict)
                and cached.get("status", "complete") == "complete"):
            return cached["profile"], "complete"
    prompt = f'''Extract only clinically explicit information from this MultiCaRe article. Do not infer, diagnose,
or turn absent information into a negative finding. Use null or [] when unsupported. Return JSON only with these
optional objects: patient, presentation, assessment, findings, summary, outcome. Keep the original wording where
possible. The response is an extraction, not medical advice. Use valid compact JSON: double-quoted keys and values,
commas between fields, no Markdown, no explanation, and no trailing commas.

Article: {article}
Abstract:\n{abstract or "(none)"}
Case narrative:\n{narrative or "(none)"}'''
    last_error: Exception | None = None
    profile: dict | None = None
    for attempt in range(3):
        retry = "" if attempt == 0 else "\nYour previous response was empty or invalid JSON. Return only one valid compact JSON object now."
        try:
            response = query_local_model(prompt + retry, model=GEMMA_MODEL, max_tokens=1600)
            profile = parse_model_json(response[0]["generated_text"])
            break
        except ValueError as exc:
            last_error = exc
    status = "complete"
    error = None
    if profile is None:
        # The source narrative and abstract are retained in every manifest
        # record. An empty structured profile is safer than hallucinating a
        # substitute when the local gateway cannot complete this one article.
        profile = {}
        status = "unavailable"
        error = str(last_error)
    temporary = cache_file.with_name(f".{cache_file.name}.{uuid.uuid4().hex}.part")
    try:
        temporary.write_text(json.dumps({"source_hash": source_hash, "profile": profile, "status": status, "error": error}, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(cache_file)
    finally:
        temporary.unlink(missing_ok=True)
    return profile, status


def metadata_map(frame: pd.DataFrame) -> dict[str, dict]:
    output: dict[str, dict] = {}
    for _, row in frame.iterrows():
        key = str(row.get("article_id", ""))
        metadata = jsonable(row.get("article_metadata", {}))
        output[key] = metadata if isinstance(metadata, dict) else {}
    return output


def records_for_article(
    article: str,
    article_images: pd.DataFrame,
    article_cxr: pd.DataFrame,
    source: Path,
    assets_dir: Path,
    cache_dir: Path,
    metadata: dict[str, dict],
    abstracts: dict[str, str],
    cases: dict[str, list],
) -> tuple[list[dict], int, str]:
    """Prepare one article independently so it can be safely run by a worker."""
    article_cxr = article_cxr.sort_values("file_id")
    article_cxr_ids = set(article_cxr["file_id"].astype(str))
    copied: dict[str, dict] = {}
    skipped_missing = 0
    for _, row in article_images.sort_values("file_id").iterrows():
        source_file = source_image_path(source, str(row["file"]))
        if not source_file.is_file():
            skipped_missing += 1
            if str(row["file_id"]) in article_cxr_ids:
                raise FileNotFoundError(f"Retained CXR asset is missing from source: {source_file}")
            continue
        asset_id, size, checksum = copy_asset(source_file, assets_dir)
        copied[str(row["file_id"])] = {
            "asset_id": asset_id,
            "source_filename": Path(str(row["file"])).name,
            "caption": str(row.get("caption") or ""),
            "license": str(row.get("license") or ""),
            "image_type": str(row.get("image_type") or ""),
            "image_subtype": str(row.get("image_subtype") or ""),
            "radiology_view": jsonable(row.get("radiology_view")),
            "labels": {
                "ml": jsonable(row.get("ml_labels_for_supervised_classification")),
                "gt": jsonable(row.get("gt_labels_for_semisupervised_classification")),
            },
            "bytes": size,
            "sha256": checksum,
        }
    narrative = "\n\n".join(
        str(case.get("case_text") or "") for case in cases.get(article, []) if isinstance(case, dict)
    )
    article_profile, enrichment_status = enrich_article(article, abstracts.get(article, ""), narrative, cache_dir)
    related = [info for key, info in copied.items() if key not in article_cxr_ids]
    records: list[dict] = []
    for _, row in article_cxr.iterrows():
        primary = copied.get(str(row["file_id"]))
        if not primary:
            continue
        base = empty_profile(article_id=article, image_id=primary["asset_id"], provenance=metadata.get(article, {}))
        profile = merge_profile(base, article_profile)
        profile["study"].update({"caption": primary["caption"], "view_position": primary["radiology_view"],
                                  "radiology_region": jsonable(row.get("radiology_region")), "storage_path": primary["asset_id"]})
        profile["tags"].update({"ml_labels": as_list(primary["labels"]["ml"]), "gt_labels": as_list(primary["labels"]["gt"])})
        records.append({"point_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"multicare-cxr:{article}:{row['file_id']}")),
                        "article_id": article, "source_file_id": str(row["file_id"]), "primary_image": primary,
                        "related_images": related, "profile": profile, "raw_abstract": abstracts.get(article, ""),
                        "raw_narrative": narrative, "enrichment_status": enrichment_status})
    return records, skipped_missing, enrichment_status


def records_for_source(source: Path, output: Path, workers: int = 1) -> tuple[list[dict], dict]:
    captions = pd.read_csv(source / "captions_and_labels.csv")
    required = {"image_subtype", "image_type", "caption", "patient_id", "file", "file_id"}
    missing = required.difference(captions.columns)
    if missing:
        raise ValueError(f"captions_and_labels.csv missing columns: {sorted(missing)}")
    cxr = captions[(captions["image_subtype"] == "x_ray") & (captions["image_type"] == "radiology") &
                   captions["caption"].fillna("").str.contains(CXR_PATTERN)].copy()
    cxr["article_id"] = cxr["patient_id"].map(article_id)
    cxr = cxr[cxr["article_id"] != ""]
    all_images = captions.copy()
    all_images["article_id"] = all_images["patient_id"].map(article_id)
    article_images = {str(article): rows for article, rows in all_images.groupby("article_id", sort=False)}
    article_cxr = {str(article): rows for article, rows in cxr.groupby("article_id", sort=False)}
    metadata = metadata_map(pd.read_parquet(source / "metadata.parquet"))
    abstracts = {str(row["article_id"]): str(row.get("abstract") or "") for _, row in pd.read_parquet(source / "abstracts.parquet").iterrows()}
    cases = {str(row["article_id"]): as_list(row.get("cases")) for _, row in pd.read_parquet(source / "cases.parquet").iterrows()}
    assets_dir, cache_dir = output / "assets", output / "enrichment-cache"
    assets_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    articles = sorted(article_cxr)
    records: list[dict] = []
    skipped_missing = 0
    unavailable_enrichments = 0
    def collect(result: tuple[list[dict], int, str]) -> None:
        nonlocal skipped_missing, unavailable_enrichments
        article_records, missing_count, enrichment_status = result
        records.extend(article_records)
        skipped_missing += missing_count
        unavailable_enrichments += enrichment_status != "complete"

    if workers == 1:
        for position, article in enumerate(articles, start=1):
            collect(records_for_article(article, article_images[article], article_cxr[article], source, assets_dir, cache_dir, metadata, abstracts, cases))
            print(f"Prepared {position}/{len(articles)} articles", flush=True)
    else:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="multicare") as executor:
            futures = {
                executor.submit(records_for_article, article, article_images[article], article_cxr[article], source, assets_dir, cache_dir, metadata, abstracts, cases): article
                for article in articles
            }
            for position, future in enumerate(as_completed(futures), start=1):
                collect(future.result())
                print(f"Prepared {position}/{len(articles)} articles", flush=True)
    # Completion order is intentionally irrelevant: the manifest must remain
    # byte-for-byte stable for a given source and enrichment cache.
    records.sort(key=lambda record: (record["article_id"], record["source_file_id"]))
    stats = {"retained_cxr_count": len(records), "retained_articles": len({record["article_id"] for record in records}),
             "missing_source_images": skipped_missing,
             "copied_assets": sum(1 for asset in assets_dir.iterdir() if asset.is_file() and not asset.name.endswith(".part")),
             "workers": workers}
    stats["unavailable_article_enrichments"] = unavailable_enrichments
    return records, stats


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=SOURCE_DEFAULT)
    parser.add_argument("--output", type=Path, default=DATASET_DEFAULT)
    parser.add_argument("--workers", type=int, default=int(os.getenv("PREPARE_WORKERS", "1")),
                        help="Concurrent article workers (default: PREPARE_WORKERS or 1).")
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    source, output = args.source.resolve(), args.output.resolve()
    required = [source / name for name in ("captions_and_labels.csv", "metadata.parquet", "abstracts.parquet", "cases.parquet")]
    if not all(path.is_file() for path in required):
        raise FileNotFoundError("MultiCaRe source tables are missing; source is never created or modified by this command")
    records, stats = records_for_source(source, output, workers=args.workers)
    manifest = {"schema_version": MANIFEST_SCHEMA_VERSION, "dataset_name": "MultiCaRe CXR", "generated_at": datetime.now(timezone.utc).isoformat(),
                "source": {"root": str(source), "cxr_rule": "radiology + x_ray + chest-X-ray/CXR caption"}, "stats": stats, "records": records}
    temporary = output / "manifest.json.part"
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(output / "manifest.json")
    print(json.dumps(stats))


if __name__ == "__main__":
    main()
