from pathlib import Path
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
