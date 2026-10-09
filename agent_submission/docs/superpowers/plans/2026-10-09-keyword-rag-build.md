# Keyword RAG Build Simplification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Retain manual-based RAG while removing the local embedding model and route all required PyPI installs through Tsinghua's mirror.

**Architecture:** Keep PDF extraction, deterministic chunking, and keyword ranking in `manual.py`; remove vector fields and semantic loading. Simplify the Dockerfile and dependency list to `langgraph`, `openai`, and `pypdf`, and update workflow audit metadata and docs to describe keyword retrieval.

**Tech Stack:** Python 3.12, LangGraph, OpenAI-compatible SDK, pypdf, Docker, pytest

**Spec:** `agent_submission/docs/superpowers/specs/2026-10-09-keyword-rag-build-design.md`

## Global Constraints

- Keep `langgraph==1.2.14`, `openai==3.26.0`, and `pypdf==6.11.0` pinned.
- Install runtime dependencies from `https://pypi.tuna.tsinghua.edu.cn/simple`.
- Remove `torch`, `transformers`, `sentence-transformers`, Qwen model downloads, vector caches, and related runtime hooks.
- Preserve deterministic manual chunking and keyword ranking, workflow prompts, validation, entrypoint, and `.dockerignore` allowlist.
- Preserve existing unrelated user changes, including `.env`, the deleted `.env.example` state, README environment guidance, and LLM defaults.

---

### Task 1: Simplify manual retrieval and workflow metadata

**Files:**
- Modify: `agent_submission/submission/scan_agent/manual.py`
- Modify: `agent_submission/submission/scan_agent/workflow.py`
- Delete: `agent_submission/submission/scan_agent/embeddings.py`
- Delete: `agent_submission/submission/scan_agent/manual_cache.py`
- Modify: `agent_submission/tests/test_manual.py`
- Delete: `agent_submission/tests/test_embeddings.py`
- Modify: `agent_submission/tests/test_workflow.py`

**Interfaces:**
- `ManualIndex.search(terms, limit=6, deadline_monotonic=None, clock=time.monotonic)` returns ranked manual chunks.
- `ManualLoadResult` contains `available`, `index`, and `error` only.
- Workflow decision metadata reports `manual.retrieval == "keyword_only"` when the manual is available.

- [ ] **Step 1: Remove embedding imports, vector fields, semantic-query arguments, model/cache loading, and cache validation from `manual.py` while preserving PDF load errors, deadlines, chunking, and keyword scores.**
- [ ] **Step 2: Remove semantic-query construction from `workflow._chunks` and record only manual availability/error plus `retrieval: keyword_only` in decision metadata.**
- [ ] **Step 3: Replace hybrid/cache tests in `test_manual.py` with keyword-only retrieval/load assertions; delete the Qwen adapter and cache test module.**
- [ ] **Step 4: Add or update workflow assertions proving available manuals record `keyword_only` retrieval and no semantic metadata fields.**

### Task 2: Reduce Docker dependencies and use the mirror

**Files:**
- Modify: `agent_submission/Dockerfile`
- Modify: `agent_submission/submission/requirements.txt`
- Modify: `agent_submission/tests/test_package.py`

**Interfaces:**
- Docker installs the three pinned runtime requirements through the Tsinghua PyPI index.
- Docker continues to copy `submission/` and run `/submission/agent_system`.

- [ ] **Step 1: Keep only the three required runtime pins in `requirements.txt`.**
- [ ] **Step 2: Remove PyTorch/Hugging Face environment setup, installs, downloads, and vector-cache construction from Dockerfile; add the Tsinghua mirror to the remaining pip install and verify only the remaining imports.**
- [ ] **Step 3: Update packaging tests for the simplified Dockerfile, dependency list, and the current deny-all/allowlist `.dockerignore`; continue asserting secrets, caches, and unlisted context roots stay excluded.**

### Task 3: Update user documentation

**Files:**
- Modify: `agent_submission/README.md`

**Interfaces:**
- Build and retrieval instructions describe Tsinghua mirror installation and keyword-only manual RAG.
- Existing `.env` usage and LLM default configuration instructions remain intact; remove the stale reference to `.env.example`, which is absent from the working tree.

- [ ] **Step 1: Replace Qwen/build-size instructions with keyword retrieval and the Tsinghua mirror details.**
- [ ] **Step 2: Clarify the `.env` build-context behavior and that the Dockerfile does not copy `.env` into the image.**
- [ ] **Step 3: Keep local run, formal evaluation, and test instructions accurate.**

### Task 4: Verify and inspect the final change

**Files:**
- Verify the files above.

- [ ] **Step 1: Run the complete offline suite from `agent_submission/` with `python -m pytest -v --basetemp .pytest-tmp/keyword-rag`.**
- [ ] **Step 2: Run `python -m compileall submission`.**
- [ ] **Step 3: Run `docker build --no-cache -t scan-agent:keyword-rag .` from `agent_submission/`; confirm the log contains the Tsinghua package index and no PyTorch or Hugging Face model download.**
- [ ] **Step 4: Run `docker run --rm --entrypoint python3 scan-agent:keyword-rag -B -c 'import openai, langgraph, pypdf'`.**
- [ ] **Step 5: Inspect the diff and confirm there are no runtime references to Qwen, Torch, Transformers, SentenceTransformers, Hugging Face, semantic cache paths, or stale README instructions.**
