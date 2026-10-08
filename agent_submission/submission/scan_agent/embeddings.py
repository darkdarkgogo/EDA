"""Local Qwen3 embeddings for the command manual's in-memory RAG index."""

from collections.abc import Callable, Sequence
from pathlib import Path
import time

from .deadline import call_with_deadline, check_deadline


QUERY_INSTRUCTION = (
    "Given scan insertion requirements, retrieve passages from the DFTEXP_Scan "
    "manual that explain the relevant commands, options, or report evidence."
)
QWEN_MODEL_REVISION = "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"
_BATCH_SIZE = 16


class QwenEmbedder:
    """Small adapter around SentenceTransformer with bounded batch encoding."""

    def __init__(self, model: object) -> None:
        self._model = model

    @classmethod
    def from_local_path(cls, path: Path) -> "QwenEmbedder":
        from sentence_transformers import SentenceTransformer

        model = SentenceTransformer(
            str(path), device="cpu", local_files_only=True,
        )
        model.max_seq_length = 2048
        return cls(model)

    def embed_documents(
        self,
        texts: Sequence[str],
        deadline_monotonic: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> tuple[tuple[float, ...], ...]:
        return self._embed(texts, prompt=None, deadline_monotonic=deadline_monotonic, clock=clock)

    def embed_query(
        self,
        text: str,
        deadline_monotonic: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> tuple[float, ...]:
        return self._embed([text], prompt=f"Instruct: {QUERY_INSTRUCTION}\nQuery: ",
                            deadline_monotonic=deadline_monotonic, clock=clock)[0]

    def _embed(
        self,
        texts: Sequence[str],
        *,
        prompt: str | None,
        deadline_monotonic: float | None,
        clock: Callable[[], float],
    ) -> tuple[tuple[float, ...], ...]:
        vectors: list[tuple[float, ...]] = []
        for offset in range(0, len(texts), _BATCH_SIZE):
            check_deadline(deadline_monotonic, clock, "manual embedding")
            batch = list(texts[offset:offset + _BATCH_SIZE])

            def encode_batch():
                return self._model.encode(
                    batch,
                    prompt=prompt,
                    normalize_embeddings=True,
                    convert_to_numpy=True,
                    show_progress_bar=False,
                    batch_size=_BATCH_SIZE,
                )

            encoded = (encode_batch() if deadline_monotonic is None else call_with_deadline(
                encode_batch, deadline_monotonic=deadline_monotonic, clock=clock,
                label="manual embedding batch",
            ))
            rows = encoded.tolist() if hasattr(encoded, "tolist") else encoded
            vectors.extend(tuple(float(value) for value in row) for row in rows)
        return tuple(vectors)
