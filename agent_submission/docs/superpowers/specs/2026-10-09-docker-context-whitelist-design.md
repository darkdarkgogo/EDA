# Docker Build Context Whitelist Design

## Goal

Limit the `agent_submission/` Docker build context to the same top-level
artifacts shown in the reference submission:

- `submission/` and all of its contents
- `.env`
- `Dockerfile`
- `README.md`
- `submission.zip`

All other files and directories are excluded by default, including tests,
development dependencies, caches, Git metadata, and future unlisted files.

## Implementation

Replace the current blacklist-style `.dockerignore` with an allowlist. The
first rule ignores everything, and later negated rules restore the five named
artifacts. Both `submission/` and `submission/**` are restored so the complete
source tree remains available to `COPY submission/ /submission/`.

The `Dockerfile` remains unchanged. Therefore `.env`, `README.md`, and
`submission.zip` are present in the build context but are not copied into the
resulting image. In particular, `.env` is not baked into an image layer.

## Verification

Verify the ignore behavior with a context-only BuildKit export or an equivalent
temporary Dockerfile, then confirm the normal Dockerfile still resolves
`submission/requirements.txt` and `submission/`. No unrelated working-tree
changes should be modified.
