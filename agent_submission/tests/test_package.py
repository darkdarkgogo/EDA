"""Static packaging checks; no Docker, license, model, or network required."""

from fnmatch import fnmatchcase
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def excluded(relative: str) -> bool:
    # The package uses only positive, component/glob exclusions, no negations.
    patterns = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    parts = relative.split("/")
    prefixes = ["/".join(parts[:index]) for index in range(1, len(parts) + 1)]
    for pattern in patterns:
        if not pattern or pattern.startswith("#"):
            continue
        pattern = pattern.rstrip("/")
        if pattern.startswith("**/"):
            if any(fnmatchcase(part, pattern[3:]) for part in parts):
                return True
        elif any(fnmatchcase(prefix, pattern) for prefix in prefixes):
            return True
    return False


def test_dockerfile_uses_official_base_and_entrypoint() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    for line in [
        "FROM scan-agent-base:ubuntu24",
        "ARG QWEN_MODEL_REVISION=97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3",
        "SCAN_AGENT_EMBEDDING_MODEL_PATH=/opt/scan-agent/models/qwen3-embedding-0.6b",
        "SCAN_AGENT_EMBEDDING_MODEL_REVISION=${QWEN_MODEL_REVISION}",
        "https://download.pytorch.org/whl/cpu",
        "import openai, langgraph, pypdf, torch, transformers, sentence_transformers",
        "snapshot_download(repo_id='Qwen/Qwen3-Embedding-0.6B'",
        "revision='${QWEN_MODEL_REVISION}'",
        "RUN rm -rf /submission/*",
        "COPY submission/ /submission/",
        "ENTRYPOINT [\"/submission/agent_system\"]",
    ]:
        assert line in dockerfile


@pytest.mark.parametrize("path", [
    ".git/config", ".env", "submission/.env.local", "nested/.env.production",
    "submission/__pycache__/main.cpython-312.pyc", "submission/main.pyc",
    ".pytest_cache/data", ".pytest-tmp/run", "nested/tests/fake_dftexp_scan.py",
    "docs/spec.md", "README.md", "requirements-dev.txt", "pytest.ini",
    "public_cases/case/input/netlist.v", "case.zip", "image.tar", "image.tar.gz",
    "image.tar.xz", "image.tgz", "image.docker", "image.img",
])
def test_nonruntime_paths_are_excluded(path: str) -> None:
    assert excluded(path)


def test_runtime_files_are_copyable() -> None:
    for path in (ROOT / "submission").rglob("*"):
        if path.is_file() and path.suffix in {".py", ".txt"}:
            assert not excluded(path.relative_to(ROOT).as_posix())
    assert not excluded("submission/agent_system")


def test_runtime_dependencies_are_exactly_pinned() -> None:
    assert (ROOT / "submission/requirements.txt").read_text(encoding="utf-8").splitlines() == [
        "langgraph==1.2.14", "openai==3.26.0", "pypdf==6.11.0",
        "torch==2.14.1", "transformers==5.19.0", "sentence-transformers==6.1.0",
    ]
    assert (ROOT / "requirements-dev.txt").read_text(encoding="utf-8").splitlines() == [
        "-r submission/requirements.txt", "pytest==9.1.1",
    ]


def test_submission_has_no_committed_or_copyable_caches_or_secrets() -> None:
    tracked = subprocess.run(["git", "ls-files", "--", "."], cwd=ROOT,
                             check=True, capture_output=True, text=True).stdout.splitlines()
    for name in tracked:
        assert not any(part in {"__pycache__", ".pytest_cache", ".pytest-tmp"}
                       or part == ".env" or (part.startswith(".env.") and part != ".env.example")
                       or part.endswith((".pyc", ".pyo")) for part in name.split("/"))
    assert not (ROOT / ".env").exists()
    for path in (ROOT / "submission").rglob("*"):
        if any(part == "__pycache__" or part.startswith(".env")
               or part.endswith((".pyc", ".pyo")) for part in path.relative_to(ROOT).parts):
            assert excluded(path.relative_to(ROOT).as_posix())


def test_entrypoint_forwards_arguments_and_prevents_runtime_bytecode() -> None:
    assert "submission/agent_system text eol=lf" in (ROOT / ".gitattributes").read_text(encoding="utf-8")
    script = (ROOT / "submission/agent_system").read_bytes()
    assert b"\r" not in script
    assert script.decode("utf-8") == (
        '#!/usr/bin/env bash\nset -euo pipefail\ncd /submission\nexec python3 -B main.py "$@"\n'
    )


def test_readme_documents_formal_local_and_failure_contract() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    for required in ["docker build -t scan-agent:phase1 .", "docker run --rm",
                     "-e LLM_API_KEY", "-e LLM_BASE_URL", "-e LLM_MODEL",
                     "-e SCANINSERTION_LICENSE_SERVER", "-e SCAN_AGENT_SKIP_READY_WAIT=1",
                     "-v /absolute/case/input:/input:ro", "-v /absolute/case/output:/output:rw",
                     "scan-agent:phase1 -input /input -output /output", ".case_ready",
                     "unsupported_netlist_repair", "decision_log.json", "final_results",
                     "tool_failure", "budget_exhausted", "invalid_model_output",
                     "no_progress", "compliance_failure"]:
        assert required in text
    for required in ["DFTEXP_SCAN_LAUNCH_MODE", "file_flag", "stdin_source",
                     "DFTEXP_REAL_SMOKE=1", "DFTEXP_SCAN_EXECUTABLE",
                     "DFTEXP_SMOKE_INPUT", "DFTEXP_SMOKE_OUTPUT"]:
        assert required in text
