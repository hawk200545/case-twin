# CaseTwin — Local MultiCaRe CXR Retrieval

CaseTwin retrieves visually similar chest X-rays from a local MultiCaRe-derived
dataset. It uses a local OpenAI-compatible gateway for clinical extraction and
interactive vision prompts, direct local MedSigLIP for image vectors, and a
local Qdrant container. The source MultiCaRe download is never modified.

## Setup

Copy `backend/.env.example` to `backend/.env` and configure the local gateway
and MedSigLIP endpoints/keys. The example includes the Compose frontend
origins (`http://localhost:8080` and `http://127.0.0.1:8080`); no Hugging Face,
GCS, or hosted Qdrant configuration is used.

Prepare the derived dataset once (or safely resume it after interruption). It
uses one worker by default; use a bounded worker count to process independent
articles concurrently:

```bash
python data_pipeline/prepare_multicare.py --workers 4
python backend/validate_multicare.py --source medical_datasets/whole_multicare_dataset
python backend/preflight_local_models.py dataset/assets/<one-asset-id>
```

The preparation command reads `medical_datasets/whole_multicare_dataset`,
retains only `radiology + x_ray + chest X-ray/CXR caption` images, copies
assets into ignored `dataset/assets`, creates `dataset/manifest.json`, and
caches each article's local-model structured extraction in
`dataset/enrichment-cache`. The manifest is the sole supported data contract;
`data_pipeline/prepare_multicare.py` is the only supported preparation path.
It replaces the former cloud/GCS schema-generation workflow.

Start with `--workers 4` and increase only if the local gateway stays healthy;
each worker issues one extraction request at a time. Completed article caches
and content-addressed assets are atomically written, so after interruption a
rerun reuses completed work. The manifest is rebuilt deterministically only
after all articles finish.

Index (the command creates the vector size from the first real embedding and
skips existing deterministic point IDs on later runs):

```bash
python backend/index_multicare.py
```

For a small fresh smoke test, set a new collection name and use `--limit 5`,
then run it twice to verify resume behaviour:

```bash
COLLECTION_NAME=multicare_cxr_smoke python backend/index_multicare.py --limit 5
COLLECTION_NAME=multicare_cxr_smoke python backend/index_multicare.py --limit 5
```

## Run locally

```bash
docker compose up --build
```

The frontend is at `http://localhost:8080`, backend health is at
`http://localhost:8005/health`, and Qdrant is at `http://localhost:6333`.
The Compose backend mounts only the derived dataset read-only; Qdrant storage
is persisted in the `qdrant_storage` Docker volume. The UI uses the one
`VITE_API_URL` configuration and receives local `/dataset-images/<asset-id>`
URLs from the API, including related article images.

Uploads for visual retrieval are JPEG, PNG, and WebP only.
