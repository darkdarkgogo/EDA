"""Static packaging checks; no Docker, license, model, or network required."""

from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def included(relative: str) -> bool:
    parts = relative.replace("\\", "/").split("/")
    if len(parts) == 1:
        return parts[0] in {"Dockerfile", ".env", "README.md", "submission.zip"}
    return parts[0] == "submission"


def test_dockerfile_uses_official_base_and_entrypoint() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    for line in [
        "FROM scan-agent-base:ubuntu24",
        "https://pypi.tuna.tsinghua.edu.cn/simple",
        "import openai, langgraph, pypdf",
        "RUN rm -rf /submission/*",
        "COPY submission/ /submission/",
        "ENTRYPOINT [\"/submission/agent_system\"]",
    ]:
        assert line in dockerfile
    assert "torch" not in dockerfile.lower()
    assert "transformers" not in dockerfile.lower()
    assert "huggingface" not in dockerfile.lower()
    assert "qwen" not in dockerfile.lower()


def test_docker_context_is_limited_to_reference_submission_roots() -> None:
    patterns = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert patterns == [
        "**", "!Dockerfile", "!.env", "!README.md", "!submission.zip",
        "!submission/", "!submission/**",
    ]
    for path in [
        "Dockerfile", ".env", "README.md", "submission.zip", "submission/agent_system",
        "submission/scan_agent/manual.py", "submission/nested/source.py",
    ]:
        assert included(path)
    for path in [
        ".dockerignore", ".git/config", ".env.example", ".pytest_cache/data",
        ".pytest-tmp/run", "tests/test_manual.py", "docs/spec.md",
        "requirements-dev.txt", "pytest.ini", "public_cases/case/input/netlist.v",
        "arbitrary.txt",
    ]:
        assert not included(path)


def test_runtime_files_are_copyable() -> None:
    for path in (ROOT / "submission").rglob("*"):
        if path.is_file() and path.suffix in {".py", ".txt"}:
            assert included(path.relative_to(ROOT).as_posix())
    assert included("submission/agent_system")


def test_runtime_dependencies_are_exactly_pinned() -> None:
    assert (ROOT / "submission/requirements.txt").read_text(encoding="utf-8").splitlines() == [
        "langgraph==1.2.14", "openai==3.26.0", "pypdf==6.11.0",
    ]
    assert (ROOT / "requirements-dev.txt").read_text(encoding="utf-8").splitlines() == [
        "-r submission/requirements.txt", "pytest==9.1.1",
    ]


def test_runtime_has_no_local_embedding_model_hooks() -> None:
    assert not (ROOT / "submission/scan_agent/embeddings.py").exists()
    assert not (ROOT / "submission/scan_agent/manual_cache.py").exists()
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "submission").rglob("*.py")
    ).casefold()
    for forbidden in ["qwen", "torch", "transformers", "sentence_transformers",
                      "huggingface", "embedding_model", "manual_index_path"]:
        assert forbidden not in source


def test_submission_has_no_committed_or_copyable_caches_or_secrets() -> None:
    tracked = subprocess.run(["git", "ls-files", "--", "."], cwd=ROOT,
                             check=True, capture_output=True, text=True).stdout.splitlines()
    for name in tracked:
        assert not any(part in {"__pycache__", ".pytest_cache", ".pytest-tmp"}
                       or part == ".env" or (part.startswith(".env.") and part != ".env.example")
                       or part.endswith((".pyc", ".pyo")) for part in name.split("/"))
    for path in (ROOT / "submission").rglob("*"):
        assert not any(part == ".env" or part.startswith(".env.")
                       for part in path.relative_to(ROOT).parts)
    assert "COPY .env" not in (ROOT / "Dockerfile").read_text(encoding="utf-8")


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
    for required in ["https://pypi.tuna.tsinghua.edu.cn/simple", "keyword_only"]:
        assert required in text
    assert "Qwen" not in text
    assert ".env.example" not in text
