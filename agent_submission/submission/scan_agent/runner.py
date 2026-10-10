"""Run the configured scan executable and retain its actual process evidence."""

from dataclasses import dataclass
import math
import os
from pathlib import Path
import subprocess
import time
from typing import Literal, Mapping, Sequence

from .artifacts import RunPaths, write_json_atomic
from .dofile import resolve_output_destinations

LaunchMode = Literal["file_flag", "stdin_source"]


@dataclass(frozen=True)
class ToolResult:
    exit_code: int | None
    timed_out: bool
    duration_seconds: float
    log_path: Path
    produced_files: tuple[str, ...]
    failure_kind: str | None = None
    failure_detail: str | None = None

    @property
    def success(self) -> bool:
        """Process success only; artifact and scan acceptance are separate checks."""
        return self.exit_code == 0 and not self.timed_out and self.failure_kind is None


def _inventory(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


def run_scan_tool(
    paths: RunPaths,
    dofile_path: Path,
    executable: Sequence[str],
    timeout_seconds: float,
    env: Mapping[str, str],
    launch_mode: LaunchMode = "file_flag",
    prepare_destinations: bool = True,
) -> ToolResult:
    """Execute without a shell, merging child output directly into the run log."""
    if not executable or isinstance(executable, (str, bytes)):
        raise ValueError("executable must be a nonempty sequence of command arguments")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be finite and greater than zero")
    if launch_mode not in {"file_flag", "stdin_source"}:
        raise ValueError("launch_mode must be 'file_flag' or 'stdin_source'")
    if prepare_destinations:
        destinations = resolve_output_destinations(dofile_path.read_text(encoding="utf-8"), paths.work)
        for destination in destinations:
            destination.parent.mkdir(parents=True, exist_ok=True)
    resolved_dofile = dofile_path.resolve()
    try:
        resolved_dofile.relative_to(paths.root.resolve())
    except ValueError as error:
        raise ValueError("dofile must be contained within its run directory") from error
    command = [*executable, "-f", str(resolved_dofile)] if launch_mode == "file_flag" else list(executable)
    stdin_text = f'Source "{resolved_dofile.as_posix()}"\nexit\n' if launch_mode == "stdin_source" else None
    merged_env = {**os.environ, **env}
    before = _inventory(paths.root)
    started = time.monotonic()
    exit_code = None
    timed_out = False
    failure_kind = None
    failure_detail = None
    # Popen passes this descriptor straight to the child. No decoding, rewriting,
    # or generated status messages can alter the tool's stdout/stderr bytes.
    with paths.log.open("w", encoding="utf-8", errors="replace") as log_handle:
        try:
            process = subprocess.Popen(
                command, cwd=paths.work, stdout=log_handle,
                stderr=subprocess.STDOUT, stdin=subprocess.PIPE if stdin_text is not None else None,
                text=True, env=merged_env,
            )
        except OSError as error:
            failure_kind = "tool_unavailable"
            failure_detail = str(error)
        else:
            try:
                if stdin_text is None:
                    process.communicate(timeout=timeout_seconds)
                else:
                    process.communicate(input=stdin_text, timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                failure_kind = "timeout"
                try:
                    process.terminate()
                except ProcessLookupError:
                    # The child can finish between timeout detection and signal.
                    pass
                try:
                    process.communicate(timeout=1.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
            exit_code = process.returncode
            if not timed_out and exit_code != 0:
                failure_kind = "nonzero_exit"
    result = ToolResult(
        exit_code=exit_code, timed_out=timed_out,
        duration_seconds=time.monotonic() - started,
        log_path=paths.log,
        produced_files=tuple(sorted(_inventory(paths.root) - before)),
        failure_kind=failure_kind, failure_detail=failure_detail,
    )
    metadata = {
        "run_id": paths.run_id,
        "command": command,
        "launch_mode": launch_mode,
        "dofile_file": resolved_dofile.relative_to(paths.root.parent.parent.resolve()).as_posix(),
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "duration_seconds": result.duration_seconds,
        "success": result.success,
        "failure_kind": result.failure_kind,
        "failure_detail": result.failure_detail,
        "log": paths.log.relative_to(paths.root).as_posix(),
        "produced_files": list(result.produced_files),
    }
    write_json_atomic(paths.root / "run_metadata.json", metadata)
    return result
