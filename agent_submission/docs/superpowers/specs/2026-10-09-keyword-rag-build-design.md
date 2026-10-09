# Keyword RAG Build Simplification Design

## Goal

Make the evaluation image build reliably within the platform's network and
time constraints while retaining retrieval-augmented generation over the Scan
User Manual. Replace the local Qwen dense-retrieval path with the existing
deterministic keyword retriever and install the remaining Python dependencies
from the Tsinghua PyPI mirror used by the reference submission.

## Selected Approach

Use keyword-only RAG and remove the local embedding implementation completely.
The agent will continue to extract and chunk the manual, rank chunks by exact
case-insensitive term frequency with a command-pattern boost, and inject the
highest-ranked chunks into the LLM prompt. This remains RAG because generation
is augmented with retrieved manual passages; only dense vector retrieval is
removed.

The rejected alternatives are leaving dormant Qwen code after removing its
packages, which creates an unusable runtime branch, and replacing Qwen with a
smaller embedding model, which preserves the same model-download and native
runtime risks that caused the evaluation build failure.

## Docker and Dependencies

`submission/requirements.txt` will retain only `langgraph`, `openai`, and
`pypdf` at their current pinned versions. `torch`, `transformers`, and
`sentence-transformers` will be removed.

The Dockerfile will:

1. copy the requirements file;
2. install it with
   `-i https://pypi.tuna.tsinghua.edu.cn/simple`;
3. verify imports for `openai`, `langgraph`, and `pypdf`;
4. copy the submission and preserve the existing entrypoint.

It will no longer install PyTorch, define embedding-model environment
variables, access Hugging Face, download Qwen weights, or build a vector cache.
The final image must not contain a local embedding model as part of this
submission.

## Runtime Retrieval

`manual.py` will own only PDF loading, section chunking, and keyword ranking.
`ManualIndex` will contain chunks without embeddings or an embedder. Its search
interface will accept terms and a result limit; callers will no longer provide
a semantic query. `ManualLoadResult` will report manual availability and PDF
errors without semantic-availability fields.

`workflow.py` will continue to request relevant manual chunks before each LLM
generation or repair. Decision metadata will report `retrieval: keyword_only`
and will remove the obsolete `semantic_available` and `semantic_error` keys.
All other workflow behavior, prompts, validation, deadlines, and output
artifacts remain unchanged.

The Qwen-specific `embeddings.py` and build-time `manual_cache.py` modules will
be deleted. No runtime import or environment lookup may refer to Qwen,
SentenceTransformers, Torch, Transformers, Hugging Face, or an embedding cache.

## Documentation and Packaging

README build instructions will describe lightweight keyword RAG, the Tsinghua
mirror, and the absence of a local model download. Existing uncommitted README
changes concerning `.env` and default LLM configuration must be preserved.
Historical design and plan documents will remain unchanged because they record
the earlier implementation state.

The Docker build context allowlist is unchanged. `.env` remains available to
the trusted builder but is not copied by the Dockerfile into the image.

## Error Handling

If the manual PDF is absent, unreadable, or exceeds its deadline, the existing
manual-unavailable result and workflow failure behavior remain in place.
Keyword search must remain deterministic and return the first ranked chunks
even when none of the requested terms occur. Removing semantic retrieval must
not introduce a new network fallback.

## Verification

Tests will cover deterministic keyword ranking, manual PDF loading and failure
paths, workflow metadata, dependency pins, Tsinghua mirror configuration, and
the absence of all Qwen build/runtime hooks. Qwen adapter/cache tests will be
deleted with their implementation.

Run the complete offline pytest suite and Python compilation. Then run a
no-cache Docker build to prove the image can install from the configured mirror
without downloading PyTorch or a Hugging Face model. Finally inspect the image
with a lightweight import command for `openai`, `langgraph`, and `pypdf`.
