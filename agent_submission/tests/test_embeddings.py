from pathlib import Path
import hashlib
import json
import sys
from types import SimpleNamespace

from scan_agent import manual_cache
from scan_agent.embeddings import QUERY_INSTRUCTION, QWEN_MODEL_REVISION, QwenEmbedder
from scan_agent.manual import ManualChunk, ManualIndex, ManualLoadResult


class _Model:
    def __init__(self):
        self.calls = []

    def encode(self, texts, **kwargs):
        self.calls.append((texts, kwargs))
        return [[1.0, 0.0] for _ in texts]


def test_qwen_adapter_encodes_passages_and_instruction_prefixed_queries():
    model = _Model()
    embedder = QwenEmbedder(model)

    docs = embedder.embed_documents(["manual passage"])
    query = embedder.embed_query("中文扫描使能")

    assert docs == ((1.0, 0.0),)
    assert query == (1.0, 0.0)
    assert model.calls[0][1]["prompt"] is None
    assert model.calls[1][1]["prompt"] == f"Instruct: {QUERY_INSTRUCTION}\nQuery: "
    assert model.calls[1][1]["normalize_embeddings"] is True
    assert model.calls[1][1]["convert_to_numpy"] is True


def test_model_loader_uses_local_path_only(monkeypatch, tmp_path: Path):
    created = {}

    class SentenceTransformer:
        def __init__(self, path, **kwargs):
            created["path"] = path
            created["kwargs"] = kwargs
            self.max_seq_length = 512

    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(
        SentenceTransformer=SentenceTransformer,
    ))

    embedder = QwenEmbedder.from_local_path(tmp_path / "qwen-model")

    assert created["path"] == str(tmp_path / "qwen-model")
    assert created["kwargs"] == {"device": "cpu", "local_files_only": True}
    assert embedder._model.max_seq_length == 2048


def test_build_cache_persists_manual_hash_and_vectors(monkeypatch, tmp_path: Path):
    pdf = tmp_path / "manual.pdf"
    pdf.write_bytes(b"manual bytes")
    cache = tmp_path / "manual-index.json"
    chunks = (ManualChunk(2, 0, "set_scan_signal"),)
    monkeypatch.setattr(manual_cache, "load_manual", lambda *args, **kwargs:
                        ManualLoadResult(True, ManualIndex(chunks, ((0.25, 0.75),)), semantic_available=True))

    assert manual_cache.build_cache(pdf, tmp_path / "model", cache) is True
    saved = json.loads(cache.read_text(encoding="utf-8"))
    assert saved == {
        "format_version": 1,
        "model_revision": QWEN_MODEL_REVISION,
        "pdf_sha256": hashlib.sha256(b"manual bytes").hexdigest(),
        "chunk_keys": [[2, 0]],
        "embeddings": [[0.25, 0.75]],
    }
