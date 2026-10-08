"""Small absolute-deadline primitives for deterministic and blocking work."""

from collections.abc import Callable
import os
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Thread
import time
from typing import TypeVar


T = TypeVar("T")


class DeadlineExceeded(TimeoutError):
    """An absolute case deadline was reached between bounded work units."""


def check_deadline(
    deadline_monotonic: float | None,
    clock: Callable[[], float] = time.monotonic,
    label: str = "operation",
) -> None:
    if deadline_monotonic is not None and clock() >= deadline_monotonic:
        raise DeadlineExceeded(f"{label} deadline reached")


def call_with_deadline(
    function: Callable[..., T],
    *args: object,
    deadline_monotonic: float,
    clock: Callable[[], float] = time.monotonic,
    label: str = "operation",
) -> T:
    """Return without joining overdue blocking work.

    The daemon worker receives only its arguments and a private queue.  After
    cancellation it cannot mutate caller-owned result state or publish a late
    outcome.
    """
    check_deadline(deadline_monotonic, clock, label)
    cancelled = Event()
    outcomes: Queue[tuple[bool, object]] = Queue(maxsize=1)

    def worker() -> None:
        try:
            value: object = function(*args)
            outcome = (True, value)
        except BaseException as error:
            outcome = (False, error)
        if not cancelled.is_set():
            try:
                outcomes.put_nowait(outcome)
            except Exception:
                pass

    Thread(target=worker, name=f"scan-agent-{label}", daemon=True).start()
    try:
        succeeded, value = outcomes.get(timeout=max(0.0, deadline_monotonic - clock()))
    except Empty:
        cancelled.set()
        raise DeadlineExceeded(f"{label} deadline reached") from None
    check_deadline(deadline_monotonic, clock, label)
    if not succeeded:
        raise value  # type: ignore[misc]
    return value  # type: ignore[return-value]


def iter_paths_with_deadline(
    root: Path,
    deadline_monotonic: float | None,
    clock: Callable[[], float] = time.monotonic,
    label: str = "filesystem traversal",
):
    """Yield a deterministic tree without following directory links."""
    pending = [root]
    while pending:
        directory = pending.pop()
        check_deadline(deadline_monotonic, clock, label)
        entries: list[os.DirEntry[str]] = []
        with os.scandir(directory) as iterator:
            for entry in iterator:
                check_deadline(deadline_monotonic, clock, label)
                entries.append(entry)
        children = []
        for entry in sorted(entries, key=lambda item: item.name):
            check_deadline(deadline_monotonic, clock, label)
            path = Path(entry.path)
            yield path
            if entry.is_dir(follow_symlinks=False):
                children.append(path)
        pending.extend(reversed(children))
