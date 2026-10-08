"""Create a build-time Qwen vector cache for the bundled tool manual."""

import hashlib
import json
import os
from pathlib import Path

from .embeddings import QWEN_MODEL_REVISION
from .manual import load_manual


MANUAL_PATH = Path("/opt/dftexp_scan/doc/Scan_User_Manual.pdf")
MODEL_PATH = Path("/opt/scan-agent/models/qwen3-embedding-0.6b")
CACHE_PATH = Path("/opt/scan-agent/manual-index.json")


def build_cache(manual_path: Path = MANUAL_PATH, model_path: Path = MODEL_PATH,
                cache_path: Path = CACHE_PATH) -> bool:
    if not manual_path.is_file():
        print(f"Manual PDF not present in base image; semantic cache skipped: {manual_path}")
        return False
    result = load_manual(
        manual_path,
        embedding_model_path=model_path,
        embedding_index_path=cache_path.with_name("manual-index.build.json"),
    )
    if not result.available or not result.semantic_available or result.index is None:
        raise RuntimeError(result.semantic_error or result.error or "manual semantic index was not built")
    payload = {
        "format_version": 1,
        "model_revision": os.environ.get("SCAN_AGENT_EMBEDDING_MODEL_REVISION", QWEN_MODEL_REVISION),
        "pdf_sha256": hashlib.sha256(manual_path.read_bytes()).hexdigest(),
        "chunk_keys": [[chunk.page, chunk.chunk_index] for chunk in result.index.chunks],
        "embeddings": result.index.embeddings,
    }
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = cache_path.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    os.replace(temp_path, cache_path)
    print(f"Qwen manual vector cache built for {len(result.index.chunks)} chunks at {cache_path}")
    return True


if __name__ == "__main__":
    build_cache()
