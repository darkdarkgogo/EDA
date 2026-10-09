# Manual Retrieval Implementation Plan

> **For agentic workers:** Implement the tasks below in order, checking each behavior with focused tests before running the full suite.

**Goal:** Retrieve relevant Scan User Manual passages with section-aware BM25F, exact command matching, and coverage across task intents.

**Architecture:** `manual.py` owns PDF cleaning, chunk metadata, scoring, and selection. A small query planner maps structured requirements and diagnostics to focused groups. `workflow.py` passes those groups into the index while the model receives the same passage references plus headings.

**Tech Stack:** Python 3.12, `pypdf`, `pytest`; no new runtime package.

**Spec:** `docs/superpowers/specs/2026-10-09-manual-retrieval-design.md`

## Global Constraints

- Keep six passages for ordinary tasks, allow up to fourteen for extra topics, and preserve stable page/chunk references.
- Preserve deadline checks and `keyword_only` audit metadata.
- Do not package the supplied PDF or add a runtime dependency.

---

### Task 1: Section-aware extraction

**Files:** `submission/scan_agent/manual.py`, `tests/test_manual.py`

**Interface:** Extend `ManualChunk` with `title: str = ""`; `_page_chunks` accepts an inherited title; `load_manual` carries it between pages.

- [ ] Add tests using Chinese numbered and unnumbered headings, repeated page headers, and a command-only example line.
- [ ] Run `python -m pytest -q tests/test_manual.py --basetemp .pytest-tmp/manual-retrieval` to observe the focused failures.
- [ ] Implement page cleaning and heading detection, keeping the original page number and 2,000-character cap.
- [ ] Re-run the focused tests.

### Task 2: BM25F and exact command ranking

**Files:** `submission/scan_agent/manual.py`, `tests/test_manual.py`

**Interface:** `ManualIndex.search(terms, limit=6, deadline_monotonic=None, clock=...) -> list[ManualChunk]`; add `search_diverse(groups, limit=6, ...)`.

- [ ] Add tests for exact command boundaries, title weighting, length normalization, no-match behavior, and stable ties.
- [ ] Run the focused tests to observe failures.
- [ ] Build title/body token statistics once per index and score query groups with BM25F plus exact command boost.
- [ ] Implement round-robin, deduplicated group selection and re-run focused tests.

### Task 3: Workflow query planning

**Files:** `submission/scan_agent/manual_queries.py`, `submission/scan_agent/workflow.py`, `submission/scan_agent/llm.py`, `tests/test_workflow.py`, `tests/test_llm.py`

**Interface:** `initial_query_groups(requirements) -> list[list[str]]`; `repair_query_groups(requirements, terms) -> list[list[str]]`.

- [ ] Add tests for initial topic coverage, diagnostic rule/command isolation, empty-diagnostic fallback, and heading serialization.
- [ ] Run these focused tests to observe failures.
- [ ] Add deterministic query planning; call `search_diverse` from the workflow and include titles in manual data.
- [ ] Re-run focused tests.

### Task 4: Real-manual verification

**Files:** `tests/test_manual_real.py`, `README.md`

**Interface:** Optional local tests read `../Scan_User_Manual.pdf` when present.

- [ ] Add optional checks for `insert_dft_logic`, `DFTR9`, and Chinese heading extraction.
- [ ] Update README retrieval description and run the real-manual test.
- [ ] Run `python -m pytest -q --basetemp .pytest-tmp/full-manual-retrieval` and `python -m compileall submission`.
- [ ] Review the final diff and preserve existing unrelated working-tree changes.
