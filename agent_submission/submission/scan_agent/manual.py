"""PDF manual extraction with deterministic keyword and local semantic retrieval."""

from dataclasses import dataclass, field
from pathlib import Path
import hashlib
import json
import math
import os
import re
import time
from collections.abc import Callable
from typing import Sequence

from pypdf import PdfReader

from .deadline import DeadlineExceeded, call_with_deadline, check_deadline
from .embeddings import QWEN_MODEL_REVISION, QwenEmbedder


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
    embeddings: tuple[tuple[float, ...], ...] = ()
    embedder: QwenEmbedder | None = field(default=None, repr=False, compare=False)

    def search(
        self,
        terms: Sequence[str],
        limit: int = 6,
        deadline_monotonic: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        semantic_query: str | None = None,
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
        if (self.embedder is None or len(self.embeddings) != len(self.chunks)
                or not self.chunks):
            return [item[3] for item in ranked[:limit]]

        query = semantic_query or " ".join(term.strip() for term in terms if term and term.strip())
        if not query:
            return [item[3] for item in ranked[:limit]]
        try:
            query_vector = self.embedder.embed_query(query, deadline_monotonic, clock)
        except DeadlineExceeded:
            raise
        except Exception:
            return [item[3] for item in ranked[:limit]]
        if not query_vector or any(len(vector) != len(query_vector) for vector in self.embeddings):
            return [item[3] for item in ranked[:limit]]
        lexical_ranks = {
            id(item[3]): rank for rank, item in enumerate(
                (entry for entry in ranked if entry[0] > 0), 1,
            )
        }
        semantic_scores = []
        for index, vector in enumerate(self.embeddings):
            check_deadline(deadline_monotonic, clock, "manual search")
            score = sum(left * right for left, right in zip(query_vector, vector))
            if not math.isfinite(score):
                return [item[3] for item in ranked[:limit]]
            semantic_scores.append((score, self.chunks[index]))
        semantic_scores.sort(key=lambda item: (-item[0], item[1].page, item[1].chunk_index))
        semantic_ranks = {id(item[1]): rank for rank, item in enumerate(semantic_scores, 1)}
        # Reciprocal-rank fusion keeps lexical identifiers useful while allowing
        # semantically equivalent Chinese/English wording to retrieve passages.
        fused = []
        for chunk in self.chunks:
            check_deadline(deadline_monotonic, clock, "manual search")
            lexical_rank = lexical_ranks.get(id(chunk))
            semantic_rank = semantic_ranks[id(chunk)]
            score = ((1.0 / (60 + lexical_rank)) if lexical_rank else 0.0) + 1.0 / (60 + semantic_rank)
            fused.append((score, chunk.page, chunk.chunk_index, chunk))
        fused.sort(key=lambda item: (-item[0], item[1], item[2]))
        check_deadline(deadline_monotonic, clock, "manual search")
        return [item[3] for item in fused[:limit]]


@dataclass(frozen=True)
class ManualLoadResult:
    available: bool
    index: ManualIndex | None
    error: str | None = None
    semantic_available: bool = False
    semantic_error: str | None = None


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
    embedder: QwenEmbedder | None = None,
    embedding_model_path: Path | None = None,
    embedding_index_path: Path | None = None,
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
    chunks_tuple = tuple(chunks)
    semantic_error = None
    if embedder is None:
        configured_path = embedding_model_path or Path(os.environ.get(
            "SCAN_AGENT_EMBEDDING_MODEL_PATH", "/opt/scan-agent/models/qwen3-embedding-0.6b",
        ))
        if configured_path.is_dir():
            try:
                embedder = (QwenEmbedder.from_local_path(configured_path) if deadline_monotonic is None else
                            call_with_deadline(
                                QwenEmbedder.from_local_path, configured_path,
                                deadline_monotonic=deadline_monotonic, clock=clock,
                                label="manual embedding model loading",
                            ))
            except DeadlineExceeded:
                semantic_error = "manual embedding model loading deadline reached"
            except Exception:
                semantic_error = "local manual embedding model is unavailable"
        else:
            semantic_error = "local manual embedding model files are unavailable"
    if embedder is not None and chunks_tuple:
        try:
            configured_index_path = embedding_index_path or Path(os.environ.get(
                "SCAN_AGENT_MANUAL_INDEX_PATH", "/opt/scan-agent/manual-index.json",
            ))
            embeddings = _load_cached_embeddings(
                path, chunks_tuple, configured_index_path, deadline_monotonic, clock,
            )
            if embeddings is None:
                embeddings = tuple(embedder.embed_documents(
                    [chunk.text for chunk in chunks_tuple], deadline_monotonic, clock,
                ))
            if len(embeddings) != len(chunks_tuple) or any(not vector for vector in embeddings):
                raise ValueError("embedding output does not match manual chunks")
            index = ManualIndex(chunks_tuple, embeddings, embedder)
            return ManualLoadResult(True, index, semantic_available=True)
        except DeadlineExceeded:
            semantic_error = "manual embedding index deadline reached"
        except Exception:
            semantic_error = "manual embedding index is unavailable"
    return ManualLoadResult(True, ManualIndex(chunks_tuple), semantic_error=semantic_error)


def _load_cached_embeddings(
    manual_path: Path,
    chunks: tuple[ManualChunk, ...],
    cache_path: Path,
    deadline_monotonic: float | None,
    clock: Callable[[], float],
) -> tuple[tuple[float, ...], ...] | None:
    """Load a build-time vector index only when it matches this exact manual."""
    if not cache_path.is_file():
        return None
    check_deadline(deadline_monotonic, clock, "manual embedding cache")
    digest = hashlib.sha256()
    with manual_path.open("rb") as source:
        while True:
            check_deadline(deadline_monotonic, clock, "manual embedding cache")
            block = source.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    check_deadline(deadline_monotonic, clock, "manual embedding cache")
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        if (not isinstance(payload, dict) or payload.get("format_version") != 1
                or payload.get("model_revision") != os.environ.get(
                    "SCAN_AGENT_EMBEDDING_MODEL_REVISION", QWEN_MODEL_REVISION,
                )
                or payload.get("pdf_sha256") != digest.hexdigest()
                or payload.get("chunk_keys") != [[chunk.page, chunk.chunk_index] for chunk in chunks]):
            return None
        raw_vectors = payload.get("embeddings")
        if not isinstance(raw_vectors, list) or len(raw_vectors) != len(chunks):
            return None
        vectors = tuple(tuple(float(value) for value in vector) for vector in raw_vectors)
        if (not vectors or any(not vector for vector in vectors)
                or len({len(vector) for vector in vectors}) != 1
                or any(not math.isfinite(value) for vector in vectors for value in vector)):
            return None
        return vectors
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
