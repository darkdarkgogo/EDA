"""Read-only input discovery, deterministic limits, and integrity checks."""

import hashlib
import math
import os
from pathlib import Path
import re
import time
from collections.abc import Callable

from .deadline import DeadlineExceeded, check_deadline
from .state import Budget, InputInventory, TaskType


_RUNTIME_EXCLUSIONS = frozenset({".case_ready", "golden.dofile", "preset_issues.json"})
_HASH_CHUNK_BYTES = 1024 * 1024
_TIME_CONTEXT = r"(?:wall[\s-]*time|total\s+(?:execution\s+)?time|time\s+(?:limit|budget)|总时间|时间限制|执行时间|耗时)"
_NUMBER = r"([+-]?\d+(?:\.\d+)?)"
_TIME_PATTERN = re.compile(
    _TIME_CONTEXT + r"[^\d+\-\n]{0,80}" + _NUMBER + r"\s*(?:seconds?\b|secs?\b|s\b|秒)",
    re.IGNORECASE,
)
_TOOL_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"(?:工具(?:调用|运行)?(?:次数)?|tool\s+(?:calls?|runs?|invocations?))[^\d+\-\n]{0,80}" + _NUMBER + r"\s*(?:次|轮|calls?\b|runs?\b|times?\b)?",
    r"dftexp_scan[^\d+\-\n]{0,80}" + _NUMBER + r"\s*(?:次|轮|calls?\b|runs?\b|times?\b)",
    _NUMBER + r"\s*(?:次|轮|calls?\b|runs?\b|invocations?\b)[^\d\n]{0,40}dftexp_scan",
))
_EXPLICIT_TOOL_LIMIT = re.compile(
    r"(?:工具(?:调用|运行)(?:次数)?|tool\s+(?:calls?|runs?|invocations?))\s*[:：]",
    re.IGNORECASE,
)


class LimitParsingError(ValueError):
    """Required wall time or an explicit tool-run limit is invalid."""


class InputMutationError(RuntimeError):
    """The protected input file set or its content changed."""


class UnsafeInputError(ValueError):
    """The input tree contains a symlink, junction, or other reparse point."""


def wait_for_case_ready(input_dir: Path, skip: bool, poll_seconds: float = 0.05) -> float:
    """Return the monotonic budget start only after readiness (or explicit skip)."""
    if not math.isfinite(poll_seconds) or poll_seconds <= 0:
        raise ValueError("poll_seconds must be finite and positive")
    if not skip:
        while not (input_dir / ".case_ready").is_file():
            time.sleep(poll_seconds)
    return time.monotonic()


def classify_task(input_dir: Path) -> TaskType:
    candidate = input_dir / "original.dofile"
    if candidate.exists() or candidate.is_symlink():
        if _is_link_or_reparse(candidate):
            raise UnsafeInputError("input symlink or reparse point is forbidden: original.dofile")
        return "task2" if candidate.is_file() else "task1"
    return "task1"


def require_semantic_input(input_dir: Path, relative: str) -> Path:
    """Recheck a semantically consumed file at the exact point of use."""
    root = input_dir.resolve()
    path = input_dir / relative
    if not path.exists() and not path.is_symlink():
        raise FileNotFoundError(f"required input is missing: {relative}")
    if _is_link_or_reparse(path):
        raise UnsafeInputError(f"input symlink or reparse point is forbidden: {relative}")
    resolved = path.resolve()
    if not resolved.is_relative_to(root) or not path.is_file():
        raise UnsafeInputError(f"semantic input is not a contained regular file: {relative}")
    return path


def parse_limits(text: str, started_at: float) -> Budget:
    """Parse seconds and runs; enforce the tightest of repeated constraints."""
    if not math.isfinite(started_at):
        raise LimitParsingError("started_at must be finite")
    seconds = [float(match.group(1)) for match in _TIME_PATTERN.finditer(text)]
    if not seconds or any(value <= 0 or not math.isfinite(value) for value in seconds):
        raise LimitParsingError("wall-time limit must specify positive seconds")
    runs = []
    for line in text.splitlines():
        values = [float(match.group(1)) for pattern in _TOOL_PATTERNS for match in pattern.finditer(line)]
        if not values and _EXPLICIT_TOOL_LIMIT.search(line):
            raise LimitParsingError("explicit tool-run limit cannot be parsed")
        if any(not math.isfinite(value) or value <= 0 or not value.is_integer() for value in values):
            raise LimitParsingError("tool-run limit must be a positive integer")
        runs.extend(int(value) for value in values)
    deadline = started_at + min(seconds)
    if not math.isfinite(deadline):
        raise LimitParsingError("wall-time deadline must be finite")
    return Budget(started_at, deadline, min(runs) if runs else 3)


def _is_link_or_reparse(path: Path) -> bool:
    stat_result = path.lstat()
    attributes = getattr(stat_result, "st_file_attributes", 0)
    return path.is_symlink() or bool(attributes & getattr(os.stat_result, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _regular_files(
    input_dir: Path,
    deadline_monotonic: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> list[Path]:
    if not input_dir.is_dir():
        raise ValueError(f"input directory does not exist: {input_dir}")
    files: list[Path] = []
    pending = [input_dir]
    while pending:
        check_deadline(deadline_monotonic, clock, "input traversal")
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                check_deadline(deadline_monotonic, clock, "input traversal")
                path = Path(entry.path)
                if entry.is_symlink() or _is_link_or_reparse(path):
                    raise UnsafeInputError(f"input symlink or reparse point is forbidden: {path.relative_to(input_dir).as_posix()}")
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                elif entry.is_file(follow_symlinks=False):
                    files.append(path)
    return sorted(files, key=lambda path: path.relative_to(input_dir).as_posix())


def inventory_inputs(
    input_dir: Path,
    deadline_monotonic: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> InputInventory:
    """List runtime paths without opening any input content."""
    return InputInventory([
        path.relative_to(input_dir).as_posix()
        for path in _regular_files(input_dir, deadline_monotonic, clock)
        if path.name not in _RUNTIME_EXCLUSIONS
    ])


def reject_input_links(
    input_dir: Path,
    deadline_monotonic: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Validate the whole tree without opening any case file contents."""
    _regular_files(input_dir, deadline_monotonic, clock)


def hash_protected_inputs(
    input_dir: Path,
    deadline_monotonic: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, str]:
    """Hash every regular input, including answer files, solely for integrity."""
    hashes = {}
    for path in _regular_files(input_dir, deadline_monotonic, clock):
        check_deadline(deadline_monotonic, clock, "input hashing")
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while True:
                check_deadline(deadline_monotonic, clock, "input hashing")
                chunk = source.read(_HASH_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
        hashes[path.relative_to(input_dir).as_posix()] = digest.hexdigest()
    return hashes


def assert_inputs_unchanged(
    input_dir: Path,
    expected: dict[str, str],
    deadline_monotonic: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    try:
        actual = hash_protected_inputs(input_dir, deadline_monotonic, clock)
    except DeadlineExceeded:
        raise
    except (OSError, ValueError) as exc:
        raise InputMutationError("protected inputs cannot be verified") from exc
    if actual != expected:
        added = sorted(actual.keys() - expected.keys())
        removed = sorted(expected.keys() - actual.keys())
        changed = sorted(key for key in actual.keys() & expected.keys() if actual[key] != expected[key])
        raise InputMutationError(f"protected inputs changed: added={added}, removed={removed}, changed={changed}")
