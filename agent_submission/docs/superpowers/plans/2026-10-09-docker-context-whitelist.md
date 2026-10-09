# Docker Build Context Whitelist Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restrict the `agent_submission/` Docker build context to the five reference-submission artifacts while preserving the existing image contents and build behavior.

**Architecture:** Replace the current blacklist with a deny-all Docker ignore rule followed by explicit allow rules. Validate the effective context through a read-only BuildKit bind mount so `.env` contents are neither printed nor copied into the resulting image.

**Tech Stack:** Docker BuildKit, `.dockerignore`, PowerShell

**Spec:** `agent_submission/docs/superpowers/specs/2026-10-09-docker-context-whitelist-design.md`

## Global Constraints

- Allow only `submission/`, `.env`, `Dockerfile`, `README.md`, and `submission.zip` from the build-context root.
- Preserve every file below `submission/`.
- Do not modify `Dockerfile`; `.env`, `README.md`, and `submission.zip` must not be copied into the final image.
- Do not modify or commit unrelated working-tree changes.

---

### Task 1: Replace and verify the Docker context filter

**Files:**
- Modify: `agent_submission/.dockerignore`
- Test: Docker's effective `agent_submission/` build context

**Interfaces:**
- Consumes: Docker ignore pattern matching for the `agent_submission/` context root.
- Produces: A context containing only the five allowed top-level artifacts and all descendants of `submission/`.

- [ ] **Step 1: Record the expected allowed roots and confirm the current filter is blacklist-based**

Run:

```powershell
Get-Content -Raw agent_submission/.dockerignore
Get-ChildItem -Force agent_submission | Select-Object -ExpandProperty Name
```

Expected: the current file lists individual exclusions, and the directory contains additional roots such as `tests`, `docs`, and development configuration files.

- [ ] **Step 2: Replace the filter with the minimal allowlist**

Set `agent_submission/.dockerignore` to exactly:

```dockerignore
**
!Dockerfile
!.env
!README.md
!submission.zip
!submission/
!submission/**
```

The directory rule makes `submission/` traversable; the recursive rule restores every descendant.

- [ ] **Step 3: Inspect the change for exactness**

Run:

```powershell
git diff --check -- agent_submission/.dockerignore
git diff -- agent_submission/.dockerignore
```

Expected: no whitespace errors and a single-file diff replacing the blacklist with the six allowlist rules.

- [ ] **Step 4: Ask Docker to enumerate the effective context without copying `.env` into an image**

Run from the repository root:

```powershell
$dockerfile = @'
# syntax=docker/dockerfile:1
FROM scan-agent-base:ubuntu24
RUN --mount=type=bind,source=.,target=/context,readonly find /context -mindepth 1 -printf '%P\n' | sort
'@
$dockerfile | docker buildx build --no-cache --progress=plain --file - agent_submission
```

Expected: every printed path is `Dockerfile`, `.env`, `README.md`, `submission.zip`, `submission`, or a descendant of `submission`. An allowed optional file may be absent when it does not exist locally. `.dockerignore` may be transferred internally by Docker to evaluate the context, but it cannot be mounted or copied from the filtered context.

- [ ] **Step 5: Confirm the production Dockerfile still references only available source paths**

Run:

```powershell
docker buildx build --check --file agent_submission/Dockerfile agent_submission
```

Expected: the Dockerfile check succeeds, including both `COPY submission/requirements.txt` and `COPY submission/`.

- [ ] **Step 6: Commit only the context-filter change and implementation plan**

Run:

```powershell
git add -- agent_submission/.dockerignore agent_submission/docs/superpowers/plans/2026-10-09-docker-context-whitelist.md
git commit -m "build: whitelist Docker context files"
```

Expected: the commit contains only `.dockerignore` and this plan; pre-existing changes remain unstaged.
