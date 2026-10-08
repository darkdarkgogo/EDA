"""Opt-in licensed smoke check; ordinary offline runs never invoke EDA tools."""

import json
import os
from pathlib import Path
import subprocess

import pytest

from scan_agent.workflow import WorkflowDependencies, run_agent


pytestmark = pytest.mark.skipif(
    os.environ.get("DFTEXP_REAL_SMOKE") != "1",
    reason="external blocker: set DFTEXP_REAL_SMOKE=1 to run licensed EDA smoke test",
)


def test_real_dftexp_minimal_case_records_help_and_agent_evidence():
    executable_value = os.environ.get("DFTEXP_SCAN_EXECUTABLE", "").strip()
    input_value = os.environ.get("DFTEXP_SMOKE_INPUT", "").strip()
    output_value = os.environ.get("DFTEXP_SMOKE_OUTPUT", "").strip()
    if not executable_value or not input_value or not output_value:
        pytest.skip("external blocker: set DFTEXP_SCAN_EXECUTABLE, DFTEXP_SMOKE_INPUT, and DFTEXP_SMOKE_OUTPUT")
    if not os.environ.get("SCANINSERTION_LICENSE_SERVER", "").strip():
        pytest.skip("external blocker: SCANINSERTION_LICENSE_SERVER is unavailable")
    if any(not os.environ.get(name, "").strip() for name in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL")):
        pytest.skip("external blocker: evaluation model configuration is unavailable")

    input_dir, output_dir = Path(input_value).resolve(), Path(output_value).resolve()
    if not input_dir.is_dir():
        pytest.skip(f"external blocker: smoke input directory is unavailable: {input_dir}")
    if not (input_dir / ".case_ready").is_file():
        pytest.skip("external blocker: minimal case is not published with .case_ready")
    executable = (executable_value,)
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        help_result = subprocess.run([*executable, "-h"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as error:
        pytest.skip(f"external blocker: dftexp_scan help command is unavailable ({type(error).__name__})")
    (output_dir / "real_tool_help.txt").write_text(
        f"command={executable_value} -h\nexit_code={help_result.returncode}\n"
        f"stdout:\n{help_result.stdout}\nstderr:\n{help_result.stderr}", encoding="utf-8",
    )
    if "not found" in (help_result.stdout + help_result.stderr).lower() or "license" in (help_result.stdout + help_result.stderr).lower() and help_result.returncode != 0:
        pytest.skip("external blocker: dftexp_scan executable or License is unavailable")

    dependencies = WorkflowDependencies.from_env()
    dependencies.executable = executable
    result = run_agent(input_dir, output_dir, dependencies)
    if result.status != "success":
        decision = output_dir / "decision_log.json"
        log_text = "".join(path.read_text(encoding="utf-8", errors="replace")
                           for path in (output_dir / "runs").glob("R*/R*.log")) if (output_dir / "runs").exists() else ""
        if "license" in (str(result.failure_reason) + log_text).lower():
            pytest.skip("external blocker: licensed dftexp_scan could not check out a License")
        pytest.fail(f"real dftexp_scan case failed; retained decision evidence at {decision}")
    payload = json.loads((output_dir / "decision_log.json").read_text(encoding="utf-8"))
    assert payload["final_run"] == result.final_run
    assert payload["status"] == "success"
    assert payload["tool_runs"]
