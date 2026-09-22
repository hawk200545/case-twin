"""Fail-fast local model preflight before a MultiCaRe batch."""
from __future__ import annotations
import argparse
from pathlib import Path
from PIL import Image
from local_ai import GEMMA_MODEL, generate_embedding, query_local_model

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("image", type=Path)
    args = parser.parse_args()
    # Some local Gemma gateways emit internal reasoning before their visible
    # answer. Sixteen tokens can exhaust the completion before that answer is
    # produced, making a healthy gateway look unavailable.
    reply = query_local_model("Reply exactly: local gateway ready", model=GEMMA_MODEL, max_tokens=1024)
    if not reply[0].get("generated_text"):
        raise RuntimeError("Local gateway returned no chat response")
    with Image.open(args.image) as image:
        vector = generate_embedding(image)
    if not vector:
        raise RuntimeError("MedSigLIP returned no vector")
    print(f"Gateway ready; normalized MedSigLIP vector dimension: {len(vector)}")

if __name__ == "__main__":
    main()
