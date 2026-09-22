"""Resumable, local-only indexing of canonical MultiCaRe CXR manifest records."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from PIL import Image
from qdrant_client import QdrantClient, models

from embedding_service import generate_embedding
from manifest import ASSET_ROOT, MANIFEST_PATH, MANIFEST_SCHEMA_VERSION


def load_manifest(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != MANIFEST_SCHEMA_VERSION or not isinstance(data.get("records"), list):
        raise ValueError(f"{path} is not a {MANIFEST_SCHEMA_VERSION} manifest")
    return data


def payload_for(record: dict) -> dict:
    return {"schema_version": MANIFEST_SCHEMA_VERSION, "profile": record["profile"],
            "primary_asset_id": record["primary_image"]["asset_id"],
            "related_asset_ids": [image["asset_id"] for image in record.get("related_images", [])],
            "primary_image": record["primary_image"], "related_images": record.get("related_images", []),
            "raw_narrative": record.get("raw_narrative", ""), "raw_abstract": record.get("raw_abstract", "")}


def index_manifest(manifest_path: Path = MANIFEST_PATH, collection: str | None = None, limit: int | None = None) -> tuple[int, int]:
    manifest = load_manifest(manifest_path)
    records = manifest["records"][:limit] if limit else manifest["records"]
    if not records:
        raise ValueError("Manifest has no retained CXR records")
    client = QdrantClient(url=os.getenv("QDRANT_URL", "http://localhost:6333"), api_key=os.getenv("QDRANT_API_KEY") or None)
    collection = collection or os.getenv("COLLECTION_NAME", "multicare_cxr")
    indexed = skipped = 0
    for record in records:
        point_id = record["point_id"]
        existing = client.retrieve(collection_name=collection, ids=[point_id], with_payload=False, with_vectors=False) if client.collection_exists(collection) else []
        if existing:
            skipped += 1
            continue
        image_path = ASSET_ROOT / record["primary_image"]["asset_id"]
        if not image_path.is_file():
            raise FileNotFoundError(f"Manifest asset is missing: {image_path}")
        with Image.open(image_path) as image:
            vector = generate_embedding(image)
        if not vector:
            raise ValueError(f"No vector for {point_id}")
        if not client.collection_exists(collection):
            client.create_collection(collection_name=collection, vectors_config=models.VectorParams(size=len(vector), distance=models.Distance.COSINE))
        client.upsert(collection_name=collection, points=[models.PointStruct(id=point_id, vector=vector, payload=payload_for(record))])
        indexed += 1
    return indexed, skipped


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--collection", default=None)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    indexed, skipped = index_manifest(args.manifest, args.collection, args.limit)
    print(f"Indexed {indexed}; resumed {skipped} existing points.")


if __name__ == "__main__":
    main()
