"""Offline integrity checks for a prepared canonical MultiCaRe dataset."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from manifest import MANIFEST_SCHEMA_VERSION


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate(manifest_path: Path, source: Path | None = None) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValueError("Unexpected manifest schema version")
    records = manifest.get("records")
    if not isinstance(records, list) or not records:
        raise ValueError("Manifest contains no CXR records")
    ids = set()
    assets = manifest_path.parent / "assets"
    for record in records:
        point_id = record.get("point_id")
        primary = record.get("primary_image", {})
        profile = record.get("profile", {})
        if not point_id or point_id in ids:
            raise ValueError(f"Missing or non-stable duplicate point_id: {point_id}")
        ids.add(point_id)
        if profile.get("profile_id") != f"{record.get('article_id')}:{primary.get('asset_id')}":
            raise ValueError(f"Profile identity mismatch for {point_id}")
        for image in [primary, *record.get("related_images", [])]:
            asset = assets / image.get("asset_id", "")
            if not asset.is_file():
                raise FileNotFoundError(f"Missing copied asset: {asset}")
            if image.get("sha256") and sha256(asset) != image["sha256"]:
                raise ValueError(f"Checksum mismatch: {asset}")
    retained = manifest.get("stats", {}).get("retained_cxr_count")
    if retained != len(records):
        raise ValueError(f"Manifest stats says {retained} CXR records but contains {len(records)}")
    report = {"records": len(records), "unique_point_ids": len(ids), "assets_checked": len(list(assets.iterdir()))}
    if source:
        import pandas as pd
        captions = pd.read_csv(source / "captions_and_labels.csv")
        expected = captions[(captions["image_subtype"] == "x_ray") & (captions["image_type"] == "radiology") &
                            captions["caption"].fillna("").str.contains(r"chest\s*x[\s-]?ray|\bcxr\b", case=False, regex=True)].shape[0]
        if expected != len(records):
            raise ValueError(f"Expected {expected} CXR rows from source rule, manifest has {len(records)}")
        report["expected_source_cxr_count"] = expected
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=Path("dataset/manifest.json"))
    parser.add_argument("--source", type=Path, default=None, help="Also verify the exact source CXR-rule count")
    args = parser.parse_args()
    print(json.dumps(validate(args.manifest, args.source), indent=2))


if __name__ == "__main__":
    main()
