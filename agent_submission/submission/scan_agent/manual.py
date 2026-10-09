"""PDF manual extraction with deterministic keyword retrieval."""

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
import math
import re
import time
from collections.abc import Callable
from typing import Sequence

from pypdf import PdfReader

from .deadline import DeadlineExceeded, call_with_deadline, check_deadline


_COMMAND = re.compile(
    r"(?:set|load|read|present|examine|insert|rpt|report|dump|write|add|remove)_[a-z0-9_]+",
    re.IGNORECASE,
)
_HEADING = re.compile(r"^#{2,6}\s+\S")
_NUMBERED_HEADING = re.compile(r"^\d+(?:\.\d+){1,5}\s+\S")
_CHAPTER_HEADINGS = frozenset({"1 Scan 引擎介绍", "2 运行 Scan 引擎", "3 附录"})
_TOKEN = re.compile(r"dftr(?:-(?:tie|l)\d+|\d+)(?:-\d+)?|[a-z][a-z0-9_]*|[\u4e00-\u9fff]+", re.IGNORECASE)
_RULE_ID = re.compile(r"dftr(?:-(?:tie|l)\d+|\d+)(?:-\d+)?", re.IGNORECASE)
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}\s+(?:\d+|[ivxlcdm]+)$", re.IGNORECASE)
_CHINESE = re.compile(r"[\u4e00-\u9fff]")
_MAX_CHUNK_CHARS = 2000


def _tokens(text: str) -> list[str]:
    result = []
    for match in _TOKEN.finditer(text.casefold()):
        word = match.group()
        if _CHINESE.fullmatch(word[0]):
            result.extend(word[index:index + 2] for index in range(len(word) - 1))
            if len(word) == 1:
                result.append(word)
        else:
            result.append(word)
    return result


def _is_heading(line: str) -> bool:
    stripped = line.strip()
    if not stripped or "......" in stripped:
        return False
    if _HEADING.match(stripped) or _is_numbered_heading(stripped):
        return True
    return (
        2 <= len(stripped) <= 36
        and bool(_CHINESE.search(stripped))
        and not stripped.startswith(("⚫", "−", "-", "#", "表 ", "图 "))
        and not any(mark in stripped for mark in ("。", "，", "；", "：", ".", ",", ";", ":", "）"))
        and (stripped.startswith(("定义", "设置", "删除", "报告", "配置", "预生成", "插入", "读入", "读取", "输出", "执行", "为 "))
             or stripped.endswith("示例"))
    )


def _is_numbered_heading(line: str) -> bool:
    return bool(_NUMBERED_HEADING.match(line)) or line in _CHAPTER_HEADINGS


def _is_page_noise(line: str) -> bool:
    value = line.strip()
    return (
        not value
        or value.startswith("Scan 引擎用户手册")
        or bool(_DATE.fullmatch(value))
        or "......" in value
    )


@dataclass(frozen=True)
class ManualChunk:
    page: int
    chunk_index: int
    text: str
    title: str = ""


@dataclass(frozen=True)
class ManualIndex:
    chunks: tuple[ManualChunk, ...]
    _build_deadline: float | None = field(default=None, repr=False, compare=False)
    _build_clock: Callable[[], float] = field(default=time.monotonic, repr=False, compare=False)
    _title_counts: tuple[Counter[str], ...] = field(init=False, repr=False, compare=False)
    _body_counts: tuple[Counter[str], ...] = field(init=False, repr=False, compare=False)
    _idf: dict[str, float] = field(init=False, repr=False, compare=False)
    _average_title_length: float = field(init=False, repr=False, compare=False)
    _average_body_length: float = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        title_counts_list = []
        body_counts_list = []
        for chunk in self.chunks:
            check_deadline(self._build_deadline, self._build_clock, "manual PDF loading")
            title_counts_list.append(Counter(_tokens(chunk.title)))
            body_counts_list.append(Counter(_tokens(chunk.text)))
        title_counts = tuple(title_counts_list)
        body_counts = tuple(body_counts_list)
        document_frequency: Counter[str] = Counter()
        for title, body in zip(title_counts, body_counts):
            check_deadline(self._build_deadline, self._build_clock, "manual PDF loading")
            document_frequency.update(title.keys() | body.keys())
        size = len(self.chunks)
        object.__setattr__(self, "_title_counts", title_counts)
        object.__setattr__(self, "_body_counts", body_counts)
        object.__setattr__(self, "_idf", {
            term: math.log1p((size - frequency + 0.5) / (frequency + 0.5))
            for term, frequency in document_frequency.items()
        })
        object.__setattr__(self, "_average_title_length", max(1.0, sum(map(lambda counts: sum(counts.values()), title_counts)) / max(size, 1)))
        object.__setattr__(self, "_average_body_length", max(1.0, sum(map(lambda counts: sum(counts.values()), body_counts)) / max(size, 1)))
        check_deadline(self._build_deadline, self._build_clock, "manual PDF loading")

    def search(
        self,
        terms: Sequence[str],
        limit: int = 6,
        deadline_monotonic: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> list[ManualChunk]:
        """Rank positive matches with title/body BM25F and exact command boost."""
        if limit <= 0:
            return []
        query_tokens: list[str] = []
        query_commands: list[str] = []
        title_phrases: list[str] = []
        for term in terms:
            check_deadline(deadline_monotonic, clock, "manual search")
            normalized = term.strip().casefold()
            if not normalized:
                continue
            query_tokens.extend(_tokens(normalized))
            if _COMMAND.fullmatch(normalized) and normalized not in query_commands:
                query_commands.append(normalized)
            elif _CHINESE.search(normalized):
                title_phrases.append(normalized)
        query_tokens = list(dict.fromkeys(query_tokens))
        if not query_tokens:
            return []
        ranked = []
        for index, chunk in enumerate(self.chunks):
            check_deadline(deadline_monotonic, clock, "manual search")
            title_counts = self._title_counts[index]
            body_counts = self._body_counts[index]
            title_length = sum(title_counts.values())
            body_length = sum(body_counts.values())
            score = 0.0
            for term in query_tokens:
                check_deadline(deadline_monotonic, clock, "manual search")
                title_tf = title_counts[term]
                body_tf = body_counts[term]
                if not title_tf and not body_tf:
                    continue
                title_norm = 1.0 - 0.3 + 0.3 * title_length / self._average_title_length
                body_norm = 1.0 - 0.75 + 0.75 * body_length / self._average_body_length
                combined_tf = 3.0 * title_tf / title_norm + body_tf / body_norm
                score += self._idf[term] * 2.2 * combined_tf / (1.2 + combined_tf)
            score += sum(
                (10.0 if position == 0 else 3.0)
                for position, command in enumerate(query_commands)
                if command in title_counts or command in body_counts
            )
            score += 30.0 * sum(phrase in chunk.title.casefold() for phrase in title_phrases)
            for rule in query_tokens:
                if _RULE_ID.fullmatch(rule) and rule in body_counts:
                    if "DRC 规则" in chunk.title:
                        score += 18.0
                    if re.search(rf"(?im)^\s*{re.escape(rule)}\s+检查", chunk.text):
                        score += 10.0
            if score > 0:
                ranked.append((score, chunk.page, chunk.chunk_index, chunk))
        check_deadline(deadline_monotonic, clock, "manual search")
        ranked.sort(key=lambda item: (-item[0], item[1], item[2]))
        check_deadline(deadline_monotonic, clock, "manual search")
        return [item[3] for item in ranked[:limit]]

    def search_diverse(
        self,
        groups: Sequence[Sequence[str]],
        limit: int = 6,
        deadline_monotonic: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> list[ManualChunk]:
        """Take one result per intent before revisiting any intent."""
        if limit <= 0:
            return []
        rankings = []
        for group in groups:
            ranking = self.search(group, len(self.chunks), deadline_monotonic, clock)
            if len(group) >= 2 and _CHINESE.search(group[0]) and _COMMAND.fullmatch(group[1]):
                command = group[1].casefold()
                command_matches = []
                for chunk in ranking:
                    check_deadline(deadline_monotonic, clock, "manual search")
                    if command in _tokens(chunk.text):
                        command_matches.append(chunk)
                if command_matches:
                    ranking = command_matches
                reference_matches = []
                for chunk in ranking:
                    check_deadline(deadline_monotonic, clock, "manual search")
                    if group[0].casefold() in chunk.title.casefold() and " > " not in chunk.title:
                        reference_matches.append(chunk)
                if reference_matches:
                    ranking = reference_matches
                elif group[0].startswith("配置"):
                    instructional_matches = []
                    for chunk in ranking:
                        check_deadline(deadline_monotonic, clock, "manual search")
                        subsection = chunk.title.split(" > ", 1)[-1]
                        if group[0].casefold() in chunk.title.casefold() and subsection.startswith(("定义", "设置")):
                            instructional_matches.append(chunk)
                    if instructional_matches:
                        ranking = instructional_matches
            rankings.append(ranking)
        offsets = [0] * len(rankings)
        selected: list[ManualChunk] = []
        seen: set[tuple[int, int]] = set()
        while len(selected) < limit:
            progress = False
            for group_index, ranking in enumerate(rankings):
                check_deadline(deadline_monotonic, clock, "manual search")
                while offsets[group_index] < len(ranking):
                    chunk = ranking[offsets[group_index]]
                    offsets[group_index] += 1
                    key = (chunk.page, chunk.chunk_index)
                    if key not in seen:
                        selected.append(chunk)
                        seen.add(key)
                        progress = True
                        break
                if len(selected) >= limit:
                    break
            if not progress:
                break
        return selected


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
    title: str = "",
) -> list[ManualChunk]:
    """Split a page on section headings while carrying its active title."""
    chunks: list[tuple[str, str]] = []
    current: list[str] = []
    active_title = title
    parent_title = title.split(" > ", 1)[0] if _is_numbered_heading(title.split(" > ", 1)[0]) else ""

    def flush() -> None:
        nonlocal current
        check_deadline(deadline_monotonic, clock, "manual page chunking")
        value = "\n".join(current).strip()
        while len(value) > _MAX_CHUNK_CHARS:
            check_deadline(deadline_monotonic, clock, "manual page chunking")
            # Prefer a nearby word boundary, but always make forward progress.
            cut = value.rfind(" ", 0, _MAX_CHUNK_CHARS + 1)
            cut = cut if cut > 0 else _MAX_CHUNK_CHARS
            chunks.append((value[:cut].rstrip(), active_title))
            value = value[cut:].lstrip()
        if value:
            chunks.append((value, active_title))
        current = []

    for line in text.splitlines():
        check_deadline(deadline_monotonic, clock, "manual page chunking")
        if _is_page_noise(line):
            continue
        if _is_heading(line):
            flush()
            heading = line.strip()
            if _is_numbered_heading(heading):
                parent_title = heading
                active_title = heading
            else:
                active_title = f"{parent_title} > {heading}" if parent_title else heading
        current.append(line.strip())
    flush()
    result = []
    for index, (chunk, chunk_title) in enumerate(chunks):
        check_deadline(deadline_monotonic, clock, "manual page chunking")
        result.append(ManualChunk(page, index, chunk, chunk_title))
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
        active_title = ""
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
            page_chunks = _page_chunks(
                text, page_number, deadline_monotonic, clock, active_title,
            )
            chunks.extend(page_chunks)
            if page_chunks:
                active_title = page_chunks[-1].title
    except DeadlineExceeded:
        return ManualLoadResult(False, None, "manual PDF loading deadline reached")
    except Exception:
        return ManualLoadResult(False, None, "manual PDF is unavailable")
    try:
        index = ManualIndex(tuple(chunks), deadline_monotonic, clock)
    except DeadlineExceeded:
        return ManualLoadResult(False, None, "manual PDF loading deadline reached")
    return ManualLoadResult(True, index)
