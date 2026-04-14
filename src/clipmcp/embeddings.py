"""
embeddings.py — Local semantic embeddings for ClipMCP v2.5.

Uses sentence-transformers (all-MiniLM-L6-v2) to generate dense vector
embeddings for clipboard content. Runs fully offline — no data leaves the
machine.

Graceful degradation: if sentence-transformers is not installed, all
functions return None/False and semantic search is disabled.

Install:
    pip install clipmcp[semantic]
    # or: pip install sentence-transformers

Model: all-MiniLM-L6-v2
  - ~80MB download (cached in ~/.cache/huggingface/)
  - 384-dimensional embeddings
  - ~5–15ms per clip on CPU
  - Very strong quality for short-to-medium text
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional import — graceful degradation if not installed
# ---------------------------------------------------------------------------

try:
    from sentence_transformers import SentenceTransformer as _SentenceTransformer
    _AVAILABLE = True
except ImportError:
    _SentenceTransformer = None  # type: ignore
    _AVAILABLE = False

MODEL_NAME = "all-MiniLM-L6-v2"
EMBEDDING_DIM = 384  # dimensions for all-MiniLM-L6-v2

_model: Optional[object] = None  # lazy-loaded singleton


def is_available() -> bool:
    """Returns True if sentence-transformers is installed and usable."""
    return _AVAILABLE


def get_model() -> Optional[object]:
    """
    Lazy-load and return the embedding model singleton.
    First call triggers the ~80MB download (cached after that).
    Returns None if sentence-transformers is not installed.
    """
    global _model

    if not _AVAILABLE:
        return None

    if _model is None:
        logger.info(f"Loading embedding model '{MODEL_NAME}' (first-time download may take a moment)...")
        try:
            _model = _SentenceTransformer(MODEL_NAME)
            logger.info(f"Embedding model '{MODEL_NAME}' loaded.")
        except Exception as e:
            logger.error(f"Failed to load embedding model: {e}")
            return None

    return _model


def embed(text: str) -> Optional[np.ndarray]:
    """
    Generate a 384-dim float32 embedding vector for the given text.
    Returns None if sentence-transformers is not installed or model fails to load.
    """
    if not text or not text.strip():
        return None

    model = get_model()
    if model is None:
        return None

    try:
        vec = model.encode(text, convert_to_numpy=True, show_progress_bar=False)
        return vec.astype(np.float32)
    except Exception as e:
        logger.warning(f"Failed to embed text: {e}")
        return None


def embed_batch(texts: list[str]) -> list[Optional[np.ndarray]]:
    """
    Embed a list of texts in one batch call (much faster than one-by-one).
    Returns a list of embeddings (or None for empty/failed items).
    """
    if not texts:
        return []

    model = get_model()
    if model is None:
        return [None] * len(texts)

    # Filter out empty texts but track their positions
    indexed = [(i, t) for i, t in enumerate(texts) if t and t.strip()]
    if not indexed:
        return [None] * len(texts)

    indices, valid_texts = zip(*indexed)

    try:
        vecs = model.encode(list(valid_texts), convert_to_numpy=True, show_progress_bar=False)
        result: list[Optional[np.ndarray]] = [None] * len(texts)
        for idx, vec in zip(indices, vecs):
            result[idx] = vec.astype(np.float32)
        return result
    except Exception as e:
        logger.warning(f"Batch embedding failed: {e}")
        return [None] * len(texts)


# ---------------------------------------------------------------------------
# Serialisation helpers for SQLite BLOB storage
# ---------------------------------------------------------------------------

def to_blob(vec: np.ndarray) -> bytes:
    """Serialise a float32 numpy array to raw bytes for SQLite BLOB storage."""
    return vec.astype(np.float32).tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    """Deserialise raw bytes from SQLite BLOB to a float32 numpy array."""
    return np.frombuffer(blob, dtype=np.float32)


# ---------------------------------------------------------------------------
# Similarity
# ---------------------------------------------------------------------------

def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """
    Compute cosine similarity between two vectors.
    Returns a float in [-1, 1]; higher = more similar.
    """
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


def rank_by_similarity(
    query_vec: np.ndarray,
    candidate_vecs: np.ndarray,  # shape: (N, EMBEDDING_DIM)
    threshold: float = 0.3,
) -> np.ndarray:
    """
    Rank N candidate vectors by cosine similarity to query_vec.

    Args:
        query_vec:      1D float32 array of shape (EMBEDDING_DIM,)
        candidate_vecs: 2D float32 array of shape (N, EMBEDDING_DIM)
        threshold:      Minimum similarity score to include in results

    Returns:
        1D float32 array of similarity scores, one per candidate.
        Scores below threshold are set to -1.0.
    """
    if candidate_vecs.ndim != 2 or candidate_vecs.shape[0] == 0:
        return np.array([], dtype=np.float32)

    # Normalise
    q_norm = query_vec / (np.linalg.norm(query_vec) + 1e-10)
    norms = np.linalg.norm(candidate_vecs, axis=1, keepdims=True) + 1e-10
    c_norm = candidate_vecs / norms

    scores = (c_norm @ q_norm).astype(np.float32)

    # Mask out below-threshold results
    scores[scores < threshold] = -1.0

    return scores


def text_for_clip(content: str, content_type: str, content_preview: str) -> Optional[str]:
    """
    Return the best text to embed for a given clip.

    - text clips: use full content
    - html clips: use content_preview (already stripped plain text)
    - image clips: return None (no text to embed)
    """
    if content_type == "image":
        return None
    if content_type == "html":
        return content_preview if content_preview else None
    # Default: plain text
    return content if content else None
