from pathlib import Path
import hashlib
import json
import threading
import time

import pytest

import scan_agent.manual as manual
from scan_agent.deadline import DeadlineExceeded
from scan_agent.manual import ManualChunk, ManualIndex, _page_chunks, load_manual


class _Page:
    def __init__(self, text: str) -> None:
        self._text = text

    def extract_text(self) -> str:
        return self._text


class _Reader:
    def __init__(self, _path: Path) -> None:
        self.pages = [
            _Page("Command Reference\nset_scan_signal configures the scan signal for DFTR9."),
            _Page("Command Reference\nset_scan_cfg sets the chain length."),
        ]


def test_manual_search_ranks_matching_command_first(monkeypatch) -> None:
    monkeypatch.setattr(manual, "PdfReader", _Reader, raising=False)

    index = load_manual(Path("manual.pdf")).index
    assert index is not None
    results = index.search(["DFTR9", "set_scan_signal"], limit=1)

    assert results[0].page == 1
    assert "set_scan_signal" in results[0].text


def test_hybrid_search_finds_english_manual_passage_for_chinese_query():
    class Embedder:
        def embed_query(self, text, deadline_monotonic=None, clock=time.monotonic):
            assert "中文扫描使能" in text
            return (1.0, 0.0)

    relevant = ManualChunk(1, 0, "Configure scan enable with set_scan_signal.")
    irrelevant = ManualChunk(2, 0, "The scan chain report lists chain lengths.")
    index = ManualIndex((relevant, irrelevant), ((1.0, 0.0), (0.0, 1.0)), Embedder())

    assert index.search(["中文扫描使能"], limit=1) == [relevant]


def test_hybrid_search_falls_back_to_keywords_when_query_embedding_fails():
    class BrokenEmbedder:
        def embed_query(self, *_args, **_kwargs):
            raise RuntimeError("model execution failed")

    exact = ManualChunk(1, 0, "set_scan_signal configuration")
    other = ManualChunk(2, 0, "scan chain output")
    index = ManualIndex((exact, other), ((1.0, 0.0), (0.0, 1.0)), BrokenEmbedder())

    assert index.search(["set_scan_signal"], limit=1) == [exact]


def test_manual_load_builds_semantic_vectors_when_embedder_is_available(monkeypatch):
    class Reader:
        def __init__(self, _path):
            self.pages = [_Page("Configure scan enable with set_scan_signal.")]

    class Embedder:
        def embed_documents(self, texts, deadline_monotonic=None, clock=time.monotonic):
            return ((1.0, 0.0) for _ in texts)

        def embed_query(self, text, deadline_monotonic=None, clock=time.monotonic):
            return (1.0, 0.0)

    monkeypatch.setattr(manual, "PdfReader", Reader)
    result = load_manual(Path("manual.pdf"), embedder=Embedder())

    assert result.available is True
    assert result.semantic_available is True
    assert result.semantic_error is None
    assert result.index is not None
    assert result.index.embeddings == ((1.0, 0.0),)


def test_manual_load_uses_cache_only_for_matching_pdf_and_chunks(monkeypatch, tmp_path):
    class Reader:
        def __init__(self, _path):
            self.pages = [_Page("Configure scan enable with set_scan_signal.")]

    class Embedder:
        def __init__(self):
            self.document_calls = 0

        def embed_documents(self, texts, deadline_monotonic=None, clock=time.monotonic):
            self.document_calls += 1
            return ((0.0, 1.0) for _ in texts)

        def embed_query(self, text, deadline_monotonic=None, clock=time.monotonic):
            return (1.0, 0.0)

    monkeypatch.setattr(manual, "PdfReader", Reader)
    pdf_path = tmp_path / "manual.pdf"
    pdf_path.write_bytes(b"stable manual")
    cache_path = tmp_path / "manual-index.json"
    cache_path.write_text(json.dumps({
        "format_version": 1,
        "model_revision": manual.QWEN_MODEL_REVISION,
        "pdf_sha256": hashlib.sha256(pdf_path.read_bytes()).hexdigest(),
        "chunk_keys": [[1, 0]],
        "embeddings": [[1.0, 0.0]],
    }), encoding="utf-8")
    embedder = Embedder()

    cached = load_manual(pdf_path, embedder=embedder, embedding_index_path=cache_path)
    assert cached.semantic_available is True
    assert cached.index is not None and cached.index.embeddings == ((1.0, 0.0),)
    assert embedder.document_calls == 0

    pdf_path.write_bytes(b"different manual")
    rebuilt = load_manual(pdf_path, embedder=embedder, embedding_index_path=cache_path)
    assert rebuilt.semantic_available is True
    assert embedder.document_calls == 1


def test_missing_manual_is_recorded_not_raised(tmp_path: Path) -> None:
    result = load_manual(tmp_path / "missing.pdf")

    assert result.available is False
    assert result.index is None
    assert result.error == "manual PDF is unavailable"


def test_unreadable_manual_is_recorded_not_raised(monkeypatch) -> None:
    def unreadable(_path: Path):
        raise OSError("missing")

    monkeypatch.setattr(manual, "PdfReader", unreadable)
    result = load_manual(Path("missing.pdf"))

    assert result.available is False
    assert result.index is None
    assert result.error == "manual PDF is unavailable"


def test_large_single_line_is_split_without_losing_text(monkeypatch) -> None:
    content = "x" * 6100

    class Reader:
        def __init__(self, _path: Path) -> None:
            self.pages = [_Page(content)]

    monkeypatch.setattr(manual, "PdfReader", Reader)
    result = load_manual(Path("manual.pdf"))

    assert result.index is not None
    chunks = result.index.chunks
    assert all(0 < len(chunk.text) <= 2000 for chunk in chunks)
    assert "".join(chunk.text for chunk in chunks) == content
    assert [chunk.chunk_index for chunk in chunks] == list(range(len(chunks)))
    assert all(chunk.page == 1 for chunk in chunks)


def test_headings_start_new_chunks(monkeypatch) -> None:
    class Reader:
        def __init__(self, _path: Path) -> None:
            self.pages = [_Page("Introduction text\n## Scan configuration\nConfigure scan.\nset_scan_signal\nSignal details.")]

    monkeypatch.setattr(manual, "PdfReader", Reader)
    result = load_manual(Path("manual.pdf"))

    assert result.index is not None
    assert [chunk.text for chunk in result.index.chunks] == [
        "Introduction text",
        "## Scan configuration\nConfigure scan.",
        "set_scan_signal\nSignal details.",
    ]


def test_search_counts_terms_case_insensitively_and_breaks_ties_stably() -> None:
    chunks = (
        ManualChunk(2, 0, "DFTR9"),
        ManualChunk(1, 1, "dftr9"),
        ManualChunk(1, 0, "Dftr9"),
        ManualChunk(3, 0, "DFTR9 dftr9"),
    )
    assert ManualIndex(chunks).search(["dFtR9"]) == [chunks[3], chunks[2], chunks[1], chunks[0]]


def test_command_in_first_200_characters_gets_bonus() -> None:
    chunks = (
        ManualChunk(1, 0, "DFTR9 " + "x" * 200 + " set_scan_signal"),
        ManualChunk(2, 0, "set_scan_signal DFTR9"),
    )
    index = ManualIndex(chunks)
    assert index.search(["DFTR9"], limit=1) == [chunks[1]]
    assert index.search(["DFTR9"], limit=0) == []
    assert index.search(["DFTR9"], limit=-1) == []


def test_extraction_failure_is_recorded_not_raised(monkeypatch) -> None:
    class BrokenPage:
        def extract_text(self) -> str:
            raise ValueError("invalid page")

    class Reader:
        def __init__(self, _path: Path) -> None:
            self.pages = [BrokenPage()]

    monkeypatch.setattr(manual, "PdfReader", Reader)
    result = load_manual(Path("manual.pdf"))
    assert result == manual.ManualLoadResult(False, None, "manual PDF is unavailable")


def test_page_without_extractable_text_is_available(monkeypatch) -> None:
    class EmptyPage:
        def extract_text(self):
            return None

    class Reader:
        def __init__(self, _path: Path) -> None:
            self.pages = [EmptyPage()]

    monkeypatch.setattr(manual, "PdfReader", Reader)
    result = load_manual(Path("manual.pdf"))
    assert result.available is True
    assert result.error is None
    assert result.index == ManualIndex(())


def test_blocked_pdf_construction_returns_at_absolute_deadline(monkeypatch, tmp_path):
    release = threading.Event()

    def blocked_reader(path):
        release.wait(2.0)
        return _Reader()

    monkeypatch.setattr("scan_agent.manual.PdfReader", blocked_reader)
    started = time.monotonic()
    try:
        result = load_manual(tmp_path / "manual.pdf", deadline_monotonic=started + 0.05)
        assert time.monotonic() - started < 0.5
        assert result.available is False
        assert result.error == "manual PDF loading deadline reached"
    finally:
        release.set()


def test_blocked_page_extraction_returns_at_absolute_deadline(monkeypatch):
    release = threading.Event()

    class BlockingPage:
        def extract_text(self):
            release.wait(2.0)
            return "late text"

    class Reader:
        def __init__(self, path):
            self.pages = [BlockingPage()]

    monkeypatch.setattr("scan_agent.manual.PdfReader", Reader)
    started = time.monotonic()
    try:
        result = load_manual(Path("manual.pdf"), deadline_monotonic=started + 0.05)
        assert time.monotonic() - started < 0.5
        assert result == manual.ManualLoadResult(False, None, "manual PDF loading deadline reached")
    finally:
        release.set()


def test_page_chunking_checks_deadline_between_lines() -> None:
    ticks = iter((0.0, 0.0, 2.0))
    with pytest.raises(DeadlineExceeded, match="manual page chunking deadline"):
        _page_chunks("one\ntwo\nthree", 1, deadline_monotonic=1.0,
                     clock=lambda: next(ticks))


def test_manual_search_checks_deadline_between_chunks() -> None:
    index = ManualIndex(tuple(ManualChunk(1, number, "scan") for number in range(3)))
    ticks = iter((0.0, 0.0, 2.0))
    with pytest.raises(DeadlineExceeded, match="manual search deadline"):
        index.search(["scan"], deadline_monotonic=1.0, clock=lambda: next(ticks))
