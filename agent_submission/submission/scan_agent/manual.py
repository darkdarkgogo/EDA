"""PDF manual extraction and deterministic, lightweight retrieval."""

from dataclasses import dataclass
from pathlib import Path
import re
import time
from collections.abc import Callable
from typing import Sequence

from pypdf import PdfReader

from .deadline import DeadlineExceeded, call_with_deadline, check_deadline


_COMMAND = re.compile(r"\b[a-z][a-z0-9]*_[a-z0-9_]+\b", re.IGNORECASE)
_HEADING = re.compile(
    r"^\s*(?:#{1,6}\s+\S.*|[A-Z][A-Z0-9 _:/()\-]{2,80}|(?i:[a-z][a-z0-9]*_[a-z0-9_]+))\s*$"
)
_MAX_CHUNK_CHARS = 2000


@dataclass(frozen=True)
class ManualChunk:
    page: int
    chunk_index: int
    text: str


@dataclass(frozen=True)
class ManualIndex:
    chunks: tuple[ManualChunk, ...]

    def search(
        self,
        terms: Sequence[str],
        limit: int = 6,
        deadline_monotonic: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> list[ManualChunk]:
        """Rank chunks by case-insensitive exact phrase counts, stably."""
        if limit <= 0:
            return []
        normalized = []
        for term in terms:
            check_deadline(deadline_monotonic, clock, "manual search")
            if term:
                normalized.append(term.casefold())
        ranked = []
        for chunk in self.chunks:
            check_deadline(deadline_monotonic, clock, "manual search")
            content = chunk.text.casefold()
            score = 0
            for term in normalized:
                check_deadline(deadline_monotonic, clock, "manual search")
                score += content.count(term)
            if _COMMAND.search(chunk.text[:200]):
                score += 1
            ranked.append((score, chunk.page, chunk.chunk_index, chunk))
        check_deadline(deadline_monotonic, clock, "manual search")
        ranked.sort(key=lambda item: (-item[0], item[1], item[2]))
        check_deadline(deadline_monotonic, clock, "manual search")
        return [item[3] for item in ranked[:limit]]


@dataclass(frozen=True)
class ManualLoadResult:
    available: bool
    index: ManualIndex | None
    error: str | None = None


def _page_chunks(
    text: str,
    page: int,
    deadline_monotonic: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> list[ManualChunk]:
    """Split page text on headings and keep chunks below the character cap."""
    chunks: list[str] = []
    current: list[str] = []

    def flush() -> None:
        nonlocal current
        check_deadline(deadline_monotonic, clock, "manual page chunking")
        value = "\n".join(current).strip()
        while len(value) > _MAX_CHUNK_CHARS:
            check_deadline(deadline_monotonic, clock, "manual page chunking")
            # Prefer a nearby word boundary, but always make forward progress.
            cut = value.rfind(" ", 0, _MAX_CHUNK_CHARS + 1)
            cut = cut if cut > 0 else _MAX_CHUNK_CHARS
            chunks.append(value[:cut].rstrip())
            value = value[cut:].lstrip()
        if value:
            chunks.append(value)
        current = []

    for line in text.splitlines():
        check_deadline(deadline_monotonic, clock, "manual page chunking")
        if _HEADING.match(line) and current:
            flush()
        current.append(line)
    flush()
    result = []
    for index, chunk in enumerate(chunks):
        check_deadline(deadline_monotonic, clock, "manual page chunking")
        result.append(ManualChunk(page, index, chunk))
    return result


def load_manual(
    path: Path,
    deadline_monotonic: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> ManualLoadResult:
    """Load a PDF manual; unavailable files are represented as data."""
    try:
        if deadline_monotonic is None:
            reader = PdfReader(path)
        else:
            reader = call_with_deadline(
                PdfReader, path, deadline_monotonic=deadline_monotonic,
                clock=clock, label="manual PDF loading",
            )
        chunks = []
        for page_number, page in enumerate(reader.pages, 1):
            check_deadline(deadline_monotonic, clock, "manual PDF loading")
            if deadline_monotonic is None:
                text = page.extract_text() or ""
            else:
                text = call_with_deadline(
                    page.extract_text, deadline_monotonic=deadline_monotonic,
                    clock=clock, label="manual PDF loading",
                ) or ""
            check_deadline(deadline_monotonic, clock, "manual PDF loading")
            chunks.extend(_page_chunks(
                text, page_number, deadline_monotonic, clock,
            ))
    except DeadlineExceeded:
        return ManualLoadResult(False, None, "manual PDF loading deadline reached")
    except Exception:
        return ManualLoadResult(False, None, "manual PDF is unavailable")
    return ManualLoadResult(True, ManualIndex(tuple(chunks)))
