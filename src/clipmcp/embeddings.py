"""
embeddings.py — Local semantic embeddings for ClipMCP v2.5.

Design: Service pattern wrapping a lazy-loaded model singleton.

The ``EmbeddingService`` class owns:
  - Lazy model loading (first call triggers ~80 MB download, cached afterwards)
  - Batch and single-text embedding
  - BLOB serialisation for SQLite storage
  - Cosine similarity computation

A module-level default instance (``_default_service``) is created at import
time.  Module-level wrapper functions delegate to it so callers that imported
the old API (``embed()``, ``is_available()``, etc.) continue to work unchanged.

Runs fully offline — no data leaves the machine.

Install:
    pip install clipmcp[semantic]
    # or: pip install sentence-transformers

Model: all-MiniLM-L6-v2
  - ~80 MB download (cached in ~/.cache/huggingface/)
  - 384-dimensional float32 embeddings
  - ~5–15 ms per clip on CPU
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np

from .models import ContentType

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional dependency — graceful degradation if not installed
# ---------------------------------------------------------------------------

try:
    from sentence_transformers import SentenceTransformer as _SentenceTransformer
    _AVAILABLE = True
except ImportError:
    _SentenceTransformer = None  # type: ignore[assignment,misc]
    _AVAILABLE = False

MODEL_NAME    = "all-MiniLM-L6-v2"
EMBEDDING_DIM = 384


# ---------------------------------------------------------------------------
# Service class
# ---------------------------------------------------------------------------

class EmbeddingService:
    """
    Manages a lazy-loaded sentence-transformer model and exposes methods for
    embedding text and computing similarity.

    The model is not loaded until the first ``embed()`` or ``embed_batch()``
    call, keeping startup cost at zero when semantic search isn't used.
    """

    def __init__(self, model_name: str = MODEL_NAME) -> None:
        self._model_name = model_name
        self._model: Optional[object] = None
        # Respect the module-level availability flag so that all instances
        # fail gracefully if sentence-transformers isn't installed.
        self._available: bool = _AVAILABLE

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Return True if sentence-transformers is installed and loadable."""
        return self._available

    # ------------------------------------------------------------------
    # Model access (lazy-loaded singleton per service instance)
    # ------------------------------------------------------------------

    def _get_model(self) -> Optional[object]:
        """
        Load and cache the model on first call.
        Subsequent calls return the cached instance (~0 ms).
        Returns None if the library is not installed or loading fails.
        """
        if not self._available:
            return None

        if self._model is None:
            logger.info(
                f"Loading embedding model '{self._model_name}' "
                "(first-time download may take a moment)..."
            )
            try:
                self._model = _SentenceTransformer(self._model_name)
                logger.info(f"Embedding model '{self._model_name}' loaded.")
            except Exception as exc:
                logger.error(f"Failed to load embedding model: {exc}")
                return None

        return self._model

    # ------------------------------------------------------------------
    # Embedding
    # ------------------------------------------------------------------

    def embed(self, text: str) -> Optional[np.ndarray]:
        """
        Generate a {dim}-dim float32 embedding for *text*.
        Returns None if the library is unavailable or the model fails.
        """.format(dim=EMBEDDING_DIM)
        if not text or not text.strip():
            return None

        model = self._get_model()
        if model is None:
            return None

        try:
            vec = model.encode(text, convert_to_numpy=True, show_progress_bar=False)
            return vec.astype(np.float32)
        except Exception as exc:
            logger.warning(f"Failed to embed text: {exc}")
            return None

    def embed_batch(self, texts: list[str]) -> list[Optional[np.ndarray]]:
        """
        Embed a list of texts in a single batch call (much faster than
        one-by-one for large backlogs).

        Empty / whitespace-only strings produce None in the output.
        The output list is always the same length as *texts*.
        """
        if not texts:
            return []

        model = self._get_model()
        if model is None:
            return [None] * len(texts)

        # Filter out empties but track their original positions so the
        # output list aligns with the input.
        indexed = [(i, t) for i, t in enumerate(texts) if t and t.strip()]
        if not indexed:
            return [None] * len(texts)

        indices, valid_texts = zip(*indexed)

        try:
            vecs = model.encode(
                list(valid_texts),
                convert_to_numpy=True,
                show_progress_bar=False,
            )
            result: list[Optional[np.ndarray]] = [None] * len(texts)
            for idx, vec in zip(indices, vecs):
                result[idx] = vec.astype(np.float32)
            return result
        except Exception as exc:
            logger.warning(f"Batch embedding failed: {exc}")
            return [None] * len(texts)

    # ------------------------------------------------------------------
    # SQLite BLOB serialisation
    # ------------------------------------------------------------------

    @staticmethod
    def to_blob(vec: np.ndarray) -> bytes:
        """Serialise a float32 numpy array to raw bytes for BLOB storage."""
        return vec.astype(np.float32).tobytes()

    @staticmethod
    def from_blob(blob: bytes) -> np.ndarray:
        """Deserialise raw BLOB bytes back to a float32 numpy array."""
        return np.frombuffer(blob, dtype=np.float32)

    # ------------------------------------------------------------------
    # Similarity
    # ------------------------------------------------------------------

    @staticmethod
    def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
        """
        Cosine similarity between two vectors.
        Returns a float in [-1, 1]; 1.0 = identical direction.
        """
        norm_a = np.linalg.norm(a)
        norm_b = np.linalg.norm(b)
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return float(np.dot(a, b) / (norm_a * norm_b))

    @staticmethod
    def rank_by_similarity(
        query_vec: np.ndarray,
        candidate_vecs: np.ndarray,
        threshold: float = 0.3,
    ) -> np.ndarray:
        """
        Score all *candidate_vecs* against *query_vec* with cosine similarity.

        Args:
            query_vec:      1-D float32 array of shape ``(EMBEDDING_DIM,)``
            candidate_vecs: 2-D float32 array of shape ``(N, EMBEDDING_DIM)``
            threshold:      Scores below this value are replaced with -1.0.

        Returns:
            1-D float32 array of scores, one per candidate.
        """
        if candidate_vecs.ndim != 2 or candidate_vecs.shape[0] == 0:
            return np.array([], dtype=np.float32)

        q_norm = query_vec / (np.linalg.norm(query_vec) + 1e-10)
        norms  = np.linalg.norm(candidate_vecs, axis=1, keepdims=True) + 1e-10
        c_norm = candidate_vecs / norms

        scores = (c_norm @ q_norm).astype(np.float32)
        scores[scores < threshold] = -1.0
        return scores

    # ------------------------------------------------------------------
    # Clip-aware text extraction
    # ------------------------------------------------------------------

    @staticmethod
    def text_for_clip(
        content: str,
        content_type: str,
        content_preview: str,
    ) -> Optional[str]:
        """
        Return the best text to embed for a given clip.

          - ``text`` clips  → use full content
          - ``html`` clips  → use content_preview (already stripped plain text)
          - ``image`` clips → return None (no embeddable text)
        """
        if content_type == ContentType.IMAGE:
            return None
        if content_type == ContentType.HTML:
            return content_preview if content_preview else None
        return content if content else None


# ---------------------------------------------------------------------------
# Module-level default service + backward-compatible function API
# ---------------------------------------------------------------------------

_default_service = EmbeddingService()


def is_available() -> bool:
    """Returns True if sentence-transformers is installed."""
    return _default_service.is_available()


def get_model() -> Optional[object]:
    """Lazy-load and return the embedding model singleton."""
    return _default_service._get_model()


def embed(text: str) -> Optional[np.ndarray]:
    """Generate a single embedding vector. Returns None on failure."""
    return _default_service.embed(text)


def embed_batch(texts: list[str]) -> list[Optional[np.ndarray]]:
    """Embed a list of texts in one batch call."""
    return _default_service.embed_batch(texts)


def to_blob(vec: np.ndarray) -> bytes:
    """Serialise embedding to SQLite BLOB bytes."""
    return EmbeddingService.to_blob(vec)


def from_blob(blob: bytes) -> np.ndarray:
    """Deserialise BLOB bytes to numpy array."""
    return EmbeddingService.from_blob(blob)


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity between two vectors."""
    return EmbeddingService.cosine_similarity(a, b)


def rank_by_similarity(
    query_vec: np.ndarray,
    candidate_vecs: np.ndarray,
    threshold: float = 0.3,
) -> np.ndarray:
    """Rank candidates by cosine similarity to query_vec."""
    return EmbeddingService.rank_by_similarity(query_vec, candidate_vecs, threshold)


def text_for_clip(
    content: str,
    content_type: str,
    content_preview: str,
) -> Optional[str]:
    """Return the best text to embed for a clip."""
    return EmbeddingService.text_for_clip(content, content_type, content_preview)
