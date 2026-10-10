from pathlib import Path

import pytest

import hashlib
import threading
import time

from scan_agent.inputs import (
    InputMutationError,
    LimitParsingError,
    UnsafeInputError,
    assert_inputs_unchanged,
    classify_task,
    hash_protected_inputs,
    inventory_inputs,
    parse_limits,
    reject_input_links,
    wait_for_case_ready,
)


def test_task2_is_detected_only_from_original_dofile(tmp_path: Path) -> None:
    (tmp_path / "original.dofile").write_text("present_design top", encoding="utf-8")
    assert classify_task(tmp_path) == "task2"


def test_public_answer_files_are_not_in_runtime_inventory(tmp_path: Path) -> None:
    for name in ("task_spec.md", "golden.dofile", "preset_issues.json", ".case_ready"):
        (tmp_path / name).write_text(name, encoding="utf-8")
    assert inventory_inputs(tmp_path).runtime_files == ["task_spec.md"]


def test_default_tool_run_limit_is_three() -> None:
    budget = parse_limits("总时间不超过 150 秒", started_at=100.0)
    assert budget.max_tool_runs == 3
    assert budget.deadline_monotonic == 250.0
    assert budget.reserve_seconds == 10.0
    assert budget.remaining(260.0) == 0.0
    assert budget.remaining(200.0) == 50.0


@pytest.mark.parametrize("text,seconds,calls", [
    ("整个 case 的执行总时间（wall time）不超过 150 秒", 150, 3),
    ("总时间不得超过 120.5 秒；最多调用 dftexp_scan 2 次", 120.5, 2),
    ("时间限制: 90秒\n工具调用次数不超过 4", 90, 4),
    ("Wall time must not exceed 180 seconds.\nAt most 5 calls to dftexp_scan.", 180, 5),
    ("Total execution time: 75 seconds\nMaximum tool runs: 2", 75, 2),
    ("Time limit: 60 s\ndftexp_scan may run at most 2 times", 60, 2),
    ("总时间不超过 100 秒，工具最多运行 2 轮", 100, 2),
    ("Total time: 150 seconds\nWall time: 120 seconds\nTool calls: 5\nTool calls: 2", 120, 2),
])
def test_chinese_and_english_limits(text: str, seconds: float, calls: int) -> None:
    budget = parse_limits(text, started_at=10.0)
    assert budget.deadline_monotonic == 10.0 + seconds
    assert budget.max_tool_runs == calls


@pytest.mark.parametrize("text", [
    "", "最多调用 dftexp_scan 3 次", "总时间不超过 0 秒",
    "总时间不超过 150 秒\n工具调用次数: 0",
    "Wall time: -10 seconds", "Wall time: 100 seconds\nTool calls: -2",
    "Wall time: 100 seconds\nTool calls: 2.5",
    "Wall time: 100 seconds\nTool calls: unknown",
])
def test_invalid_limits_fail_closed(text: str) -> None:
    with pytest.raises(LimitParsingError):
        parse_limits(text, started_at=0.0)


def test_task1_does_not_use_answer_files_or_directory(tmp_path: Path) -> None:
    (tmp_path / "golden.dofile").write_text("original.dofile", encoding="utf-8")
    (tmp_path / "preset_issues.json").write_text("task2", encoding="utf-8")
    (tmp_path / "original.dofile").mkdir()
    assert classify_task(tmp_path) == "task1"


def test_inventory_is_recursive_sorted_and_posix(tmp_path: Path) -> None:
    (tmp_path / "netlist").mkdir()
    (tmp_path / "netlist" / "design.v").write_text("module top; endmodule")
    (tmp_path / "netlist" / "golden.dofile").write_text("answer")
    (tmp_path / "task_spec.md").write_text("task")
    assert inventory_inputs(tmp_path).runtime_files == ["netlist/design.v", "task_spec.md"]


@pytest.mark.parametrize("change", ["modified", "added", "deleted"])
def test_changed_input_is_rejected(tmp_path: Path, change: str) -> None:
    source = tmp_path / "task_spec.md"
    source.write_text("before", encoding="utf-8")
    expected = hash_protected_inputs(tmp_path)
    assert_inputs_unchanged(tmp_path, expected)
    if change == "modified":
        source.write_text("after", encoding="utf-8")
    elif change == "added":
        (tmp_path / "new.lib").write_text("new", encoding="utf-8")
    else:
        source.unlink()
    with pytest.raises(InputMutationError, match="protected inputs changed"):
        assert_inputs_unchanged(tmp_path, expected)


def test_ready_wait_starts_budget_only_after_sentinel(tmp_path: Path) -> None:
    observed = []
    finished = threading.Event()

    def wait() -> None:
        observed.append(wait_for_case_ready(tmp_path, skip=False, poll_seconds=0.005))
        finished.set()

    worker = threading.Thread(target=wait, daemon=True)
    worker.start()
    try:
        assert not finished.wait(0.03)
        created_at = time.monotonic()
        (tmp_path / ".case_ready").touch()
        assert finished.wait(2.0)
        assert created_at <= observed[0] <= time.monotonic()
    finally:
        (tmp_path / ".case_ready").touch()
        worker.join(timeout=2.0)


def test_skip_wait_returns_without_reading_inputs(tmp_path: Path, monkeypatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("skip must not check sentinel or sleep")

    monkeypatch.setattr(Path, "is_file", forbidden)
    monkeypatch.setattr("scan_agent.inputs.time.sleep", forbidden)
    before = time.monotonic()
    assert before <= wait_for_case_ready(tmp_path, skip=True) <= time.monotonic()


def test_inventory_never_opens_input_contents(tmp_path: Path, monkeypatch) -> None:
    for name in ("task_spec.md", "golden.dofile", "preset_issues.json"):
        (tmp_path / name).write_text("content")

    def forbidden(*args, **kwargs):
        raise AssertionError("inventory must not open files")

    monkeypatch.setattr(Path, "open", forbidden)
    assert inventory_inputs(tmp_path).runtime_files == ["task_spec.md"]


def test_hashes_skip_preset_issues_and_stream_other_files(tmp_path: Path, monkeypatch) -> None:
    content = b"x" * (2 * 1024 * 1024 + 13)
    names = [".case_ready", "golden.dofile", "preset_issues.json", "netlist.v"]
    for name in names:
        (tmp_path / name).write_bytes(content)
    real_open = Path.open
    reads = []

    class TrackedReader:
        def __init__(self, source):
            self.source = source

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.source.close()

        def read(self, size):
            reads.append(size)
            return self.source.read(size)

    def tracked_open(path, mode="r", *args, **kwargs):
        assert mode == "rb"
        assert path.name != "preset_issues.json"
        return TrackedReader(real_open(path, mode, *args, **kwargs))

    monkeypatch.setattr(Path, "open", tracked_open)
    actual = hash_protected_inputs(tmp_path)
    protected = sorted(set(names) - {"preset_issues.json"})
    assert list(actual) == protected
    assert actual == {name: hashlib.sha256(content).hexdigest() for name in protected}
    assert reads == [1024 * 1024] * 12


def test_missing_input_directory_is_not_an_empty_valid_inventory(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(ValueError):
        inventory_inputs(missing)
    with pytest.raises(InputMutationError):
        assert_inputs_unchanged(missing, {})


@pytest.mark.parametrize("relative,target_kind", [
    ("task_spec.md", "external"),
    ("netlist/design.v", "external"),
    ("lib/stdcells.lib", "external"),
    ("task_spec.md", "answer"),
])
def test_any_input_symlink_is_rejected_before_inventory(relative, target_kind, tmp_path):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    link = input_dir / relative
    link.parent.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "external.txt" if target_kind == "external" else input_dir / "golden.dofile"
    target.write_text("secret", encoding="utf-8")
    try:
        link.symlink_to(target)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")
    with pytest.raises(UnsafeInputError, match="symlink or reparse"):
        inventory_inputs(input_dir)
    with pytest.raises(UnsafeInputError, match="symlink or reparse"):
        hash_protected_inputs(input_dir)


def test_hashing_checks_absolute_deadline_between_chunks(tmp_path):
    (tmp_path / "large.bin").write_bytes(b"x" * (2 * 1024 * 1024))
    ticks = iter((0.0, 0.0, 2.0))
    with pytest.raises(TimeoutError, match="input hashing deadline"):
        hash_protected_inputs(tmp_path, deadline_monotonic=1.0, clock=lambda: next(ticks))


def test_inventory_checks_absolute_deadline_between_entries(tmp_path):
    (tmp_path / "one.txt").write_text("one", encoding="utf-8")
    (tmp_path / "two.txt").write_text("two", encoding="utf-8")
    ticks = iter((0.0, 0.0, 2.0))
    with pytest.raises(TimeoutError, match="input traversal deadline"):
        inventory_inputs(tmp_path, deadline_monotonic=1.0, clock=lambda: next(ticks))


def test_link_rejection_checks_absolute_startup_deadline(tmp_path):
    (tmp_path / "one.txt").write_text("one", encoding="utf-8")
    (tmp_path / "two.txt").write_text("two", encoding="utf-8")
    ticks = iter((0.0, 0.0, 2.0))
    with pytest.raises(TimeoutError, match="input traversal deadline"):
        reject_input_links(tmp_path, deadline_monotonic=1.0, clock=lambda: next(ticks))


def test_reported_reparse_entry_is_rejected_without_opening(monkeypatch, tmp_path):
    class Entry:
        path = str(tmp_path / "task_spec.md")

        @staticmethod
        def is_symlink():
            return True

    class Entries:
        def __enter__(self):
            return iter((Entry(),))

        def __exit__(self, *args):
            return None

    monkeypatch.setattr("scan_agent.inputs.os.scandir", lambda path: Entries())
    with pytest.raises(UnsafeInputError, match="symlink or reparse"):
        inventory_inputs(tmp_path)
