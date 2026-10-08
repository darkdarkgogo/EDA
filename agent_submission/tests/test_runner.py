import json
from pathlib import Path
import sys

import pytest

import scan_agent.runner as runner
from scan_agent.artifacts import create_run, write_run_dofile
from scan_agent.runner import run_scan_tool


FAKE_TOOL = Path(__file__).with_name("fake_dftexp_scan.py")


def _run(tmp_path, mode, timeout=5.0, env=None):
    paths = create_run(tmp_path, 1)
    dofile = write_run_dofile(paths, "exit\n")
    result = run_scan_tool(paths, dofile, [sys.executable, str(FAKE_TOOL), "--mode", mode], timeout, env or {})
    return paths, result


def test_runner_success_and_created_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv("SCAN_TEST_INHERITED", "parent")
    monkeypatch.setenv("SCAN_TEST_OVERRIDE", "parent")
    paths, result = _run(tmp_path, "success", env={"SCAN_TEST_OVERRIDE": "child"})
    assert result.success is True
    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.failure_kind is None
    assert result.duration_seconds >= 0
    assert result.log_path == paths.log
    assert "deliverables/post_scan.v" in result.produced_files
    assert "reports/scan.rpt" in result.produced_files
    assert "deliverables/R1.dofile" not in result.produced_files
    assert (paths.work / "cwd.txt").read_text(encoding="utf-8") == str(paths.work.resolve())
    assert (paths.work / "environment.txt").read_text(encoding="utf-8") == "parent/child"
    metadata = json.loads((paths.root / "run_metadata.json").read_text(encoding="utf-8"))
    assert metadata["exit_code"] == 0
    assert metadata["success"] is True
    assert metadata["produced_files"] == list(result.produced_files)
    assert metadata["command"][-2:] == ["-f", str(paths.dofile.resolve())]


def test_runner_preserves_nonzero_exit_and_log(tmp_path):
    paths, result = _run(tmp_path, "error")
    assert result.exit_code == 7
    assert result.success is False
    assert result.failure_kind == "nonzero_exit"
    assert "[ERROR] requested failure" in paths.log.read_text(encoding="utf-8")


def test_runner_marks_timeout_without_success(tmp_path):
    paths, result = _run(tmp_path, "sleep", timeout=0.05)
    assert result.timed_out is True
    assert result.success is False
    assert result.failure_kind == "timeout"
    assert result.exit_code is not None
    assert result.duration_seconds < 5
    assert json.loads((paths.root / "run_metadata.json").read_text(encoding="utf-8"))["timed_out"] is True


def test_runner_missing_executable_is_unavailable(tmp_path):
    paths = create_run(tmp_path, 1)
    result = run_scan_tool(paths, write_run_dofile(paths, "exit"), [str(tmp_path / "missing-tool")], 5, {})
    assert result.failure_kind == "tool_unavailable"
    assert result.exit_code is None
    assert result.success is False
    assert result.timed_out is False
    assert paths.log.read_bytes() == b""
    metadata = json.loads((paths.root / "run_metadata.json").read_text(encoding="utf-8"))
    assert metadata["failure_kind"] == "tool_unavailable"
    assert metadata["failure_detail"]


def test_runner_preserves_raw_merged_output(tmp_path):
    paths, result = _run(tmp_path, "raw")
    assert result.success is True
    assert paths.log.read_bytes() == b"stdout\r\n\xff\nstderr\r\n"


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_runner_rejects_invalid_timeout(tmp_path, timeout):
    paths = create_run(tmp_path, 1)
    with pytest.raises(ValueError, match="timeout"):
        run_scan_tool(paths, write_run_dofile(paths, "exit"), [sys.executable], timeout, {})


def test_runner_requires_executable(tmp_path):
    paths = create_run(tmp_path, 1)
    with pytest.raises(ValueError, match="executable"):
        run_scan_tool(paths, write_run_dofile(paths, "exit"), [], 5, {})


def test_runner_uses_shared_atomic_metadata_writer(tmp_path, monkeypatch):
    writes = []
    original = runner.write_json_atomic

    def record_write(path, payload):
        writes.append(path)
        return original(path, payload)

    monkeypatch.setattr(runner, "write_json_atomic", record_write)
    paths, _ = _run(tmp_path, "success")
    assert writes == [paths.root / "run_metadata.json"]


def test_runner_kills_and_reaps_when_termination_times_out(tmp_path, monkeypatch):
    calls = []

    class StubbornProcess:
        returncode = None

        def communicate(self, timeout=None):
            calls.append(("communicate", timeout))
            if timeout is not None:
                raise runner.subprocess.TimeoutExpired("fake", timeout)
            self.returncode = -9

        def terminate(self):
            calls.append(("terminate",))

        def kill(self):
            calls.append(("kill",))

    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: StubbornProcess())
    _, result = _run(tmp_path, "sleep")
    assert calls == [("communicate", 5.0), ("terminate",), ("communicate", 1.0), ("kill",), ("communicate", None)]
    assert result.timed_out is True
    assert result.exit_code == -9
    assert result.success is False
