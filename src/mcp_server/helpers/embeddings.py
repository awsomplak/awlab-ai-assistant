"""
Embedding service — optional fastembed-based embedding with BM25 fallback.

Provides:
  - ``EmbeddingService`` singleton: lazy model loading, embedding computation,
    and a hybrid BM25 + dense scorer with reciprocal rank fusion.
  - Graceful fallback to keyword-only scoring when fastembed is not installed.
  - On-disk cache at ``~/.awlab-id/agent-memory/embeddings/``.
"""

from __future__ import annotations

import math
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any

from ..config import settings
from .logger import logger as _logger

log = _logger.tool("embeddings")

# ═══════════════════════════════════════════════════════════════════════════
#  FastEmbed detection
# ═══════════════════════════════════════════════════════════════════════════

_HAS_FASTEMBED: bool | None = None

try:
    from fastembed import TextEmbedding  # type: ignore[import-untyped]  # noqa: F401 — intentional availability probe

    _HAS_FASTEMBED = True
except ImportError:
    _HAS_FASTEMBED = False


def has_fastembed() -> bool:
    """Return True if the fastembed package is installed."""
    global _HAS_FASTEMBED
    if _HAS_FASTEMBED is None:
        try:
            from fastembed import TextEmbedding  # type: ignore[import-untyped]  # noqa: F401

            _HAS_FASTEMBED = True
        except ImportError:
            _HAS_FASTEMBED = False
    return _HAS_FASTEMBED


# ═══════════════════════════════════════════════════════════════════════════
#  Paths
# ═══════════════════════════════════════════════════════════════════════════


def _models_dir() -> Path:
    """Return the model storage directory (created if needed)."""
    path = settings.config_home / "models"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ═══════════════════════════════════════════════════════════════════════════
#  EmbeddingService
# ═══════════════════════════════════════════════════════════════════════════

_MODEL_NAME = "BAAI/bge-small-en-v1.5"


def ensure_model_downloaded() -> bool:
    """Download the embedding model at startup if fastembed is installed.

    Checks whether the model directory already exists in
    ``~/.awlab-id/agent-memory/models/BAAI/bge-small-en-v1.5/``.
    If missing, triggers a download via the TextEmbedding constructor
    with ``cache_dir`` pointing to the models directory.

    Returns:
        True if the model is available (downloaded or already present),
        False if fastembed is not installed or download failed.
    """
    if not has_fastembed():
        log.info("fastembed not installed — dense embedding disabled")
        return False

    cache_dir = _models_dir()
    # HuggingFace cache uses "models--org--name" format
    hf_name = _MODEL_NAME.replace("/", "--")
    model_dir = cache_dir / f"models--{hf_name}"
    snapshots_dir = model_dir / "snapshots"
    if model_dir.exists() and snapshots_dir.exists() and any(snapshots_dir.iterdir()):
        log.info(f"Model {_MODEL_NAME} already cached")
        return True

    log.info(f"Downloading model {_MODEL_NAME}…")
    try:
        from fastembed import TextEmbedding as _TE  # type: ignore[import-untyped]

        _TE(model_name=_MODEL_NAME, cache_dir=str(cache_dir), download_only=True)
        log.info("Model downloaded successfully")
        return True
    except Exception as exc:
        log.warning(f"Model download failed: {exc}")
        return False


class EmbeddingService:
    """Lazy-loaded singleton for text embeddings.

    Usage::

        svc = EmbeddingService.get_instance()
        vec = svc.compute_embedding("some text")
        vecs = svc.compute_embeddings_batch(["text a", "text b"])
    """

    _instance: EmbeddingService | None = None

    def __init__(self) -> None:
        self._model: Any = None
        self._loaded = False

    # ── Singleton ──────────────────────────────────────────────────────────

    @classmethod
    def get_instance(cls) -> EmbeddingService:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── Model loading ──────────────────────────────────────────────────────

    def _load_model(self) -> None:
        """Download (if needed) and load the embedding model."""
        if self._loaded:
            return
        self._loaded = True

        if not has_fastembed():
            log.info("fastembed not installed — embedding disabled, BM25-only mode")
            return

        cache_dir = str(_models_dir())
        try:
            from fastembed import TextEmbedding as _TE  # type: ignore[import-untyped]  # noqa: F811

            log.info(f"Loading model {_MODEL_NAME} (cache: {cache_dir})…")
            self._model = _TE(
                model_name=_MODEL_NAME,
                cache_dir=cache_dir,
                threads=min(os.cpu_count() or 4, 8),
            )
            log.info("Model loaded successfully")
        except Exception as exc:
            log.error(f"Failed to load model: {exc}")
            self._model = None

    @property
    def available(self) -> bool:
        """True when the model is loaded and ready."""
        return self._model is not None

    # ── Embedding computation ──────────────────────────────────────────────

    def compute_embedding(self, text: str) -> list[float]:
        """Return a single embedding vector for *text*.

        Raises ``RuntimeError`` if fastembed is not available.
        """
        if not has_fastembed():
            raise RuntimeError("fastembed is not installed. Install with: pip install awlab-mcp-server[hybrid]")
        self._load_model()
        if not self.available:
            raise RuntimeError("Embedding model failed to load")

        # TextEmbedder returns a generator of numpy arrays
        vec = list(self._model.embed([text]))[0]
        return vec.tolist() if hasattr(vec, "tolist") else list(vec)

    def compute_embeddings_batch(self, texts: list[str]) -> list[list[float]]:
        """Return embedding vectors for a batch of texts."""
        if not has_fastembed():
            raise RuntimeError("fastembed is not installed. Install with: pip install awlab-mcp-server[hybrid]")
        self._load_model()
        if not self.available:
            raise RuntimeError("Embedding model failed to load")

        vecs = list(self._model.embed(texts))
        return [v.tolist() if hasattr(v, "tolist") else list(v) for v in vecs]


def re_rank_results(
    query: str,
    texts: list[str],
    ids: list[str],
    original_entities: list[dict],
) -> list[dict]:
    """Re-rank entities by hybrid BM25 + dense search scores.

    Args:
        query: The search query.
        texts: Document texts (one per entity, combined from name + observations).
        ids: Entity identifiers (parallel to texts).
        original_entities: Original entity dicts from agent-recall.

    Returns:
        Entities re-ordered by descending hybrid score.
    """
    ranked = hybrid_search(query, texts, document_ids=ids, use_dense=True)
    # Build a lookup from id → entity
    entity_map = {e.get("name", "") or str(i): e for i, e in enumerate(original_entities)}
    result = []
    for r in ranked:
        eid = r["id"]
        if eid in entity_map:
            ent = dict(entity_map[eid])
            ent["score"] = r["score"]
            ent["bm25_score"] = r["bm25_score"]
            ent["dense_score"] = r["dense_score"]
            result.append(ent)
    # Append any entities not in ranked results
    seen = {r["id"] for r in ranked}
    for ent in original_entities:
        name = ent.get("name", "") or ""
        if name not in seen:
            result.append(ent)
    return result


# ═══════════════════════════════════════════════════════════════════════════
#  Hybrid Search (BM25 + Dense RRF)
# ═══════════════════════════════════════════════════════════════════════════


class BM25Scorer:
    """Lightweight BM25-OKAPI scorer using standard library only.

    Scores are computed from token frequencies in the corpus.  No external
    NLP dependency is required — tokenization is simple whitespace + lower.
    """

    def __init__(self, corpus: list[str], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self._build(corpus)

    # ── Tokenisation ───────────────────────────────────────────────────────

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        return re.findall(r"[a-z0-9]+", text.lower())

    # ── Index builder ──────────────────────────────────────────────────────

    def _build(self, corpus: list[str]) -> None:
        self.N = len(corpus)
        self.avgdl: float = 0.0
        self.doc_freqs: list[Counter] = []
        self.df: Counter = Counter()  # document frequency per term
        doc_lengths: list[int] = []

        for doc in corpus:
            tokens = self._tokenize(doc)
            freq = Counter(tokens)
            self.doc_freqs.append(freq)
            doc_lengths.append(len(tokens))
            for term in freq:
                self.df[term] += 1

        if doc_lengths:
            self.avgdl = sum(doc_lengths) / len(doc_lengths)

    # ── Scoring ────────────────────────────────────────────────────────────

    def score(self, query: str) -> list[float]:
        """Return BM25 scores for each document in the corpus against *query*."""
        query_terms = self._tokenize(query)
        if not query_terms:
            return [0.0] * self.N

        scores = [0.0] * self.N
        for term in query_terms:
            idf = self._idf(term)
            if idf == 0.0:
                continue
            for i, freq in enumerate(self.doc_freqs):
                tf = freq.get(term, 0)
                if tf == 0:
                    continue
                dl = sum(freq.values())
                numerator = tf * (self.k1 + 1)
                denominator = tf + self.k1 * (1 - self.b + self.b * dl / self.avgdl)
                scores[i] += idf * numerator / denominator

        return scores

    def _idf(self, term: str) -> float:
        n = self.df.get(term, 0)
        if n == 0:
            return 0.0
        return math.log(1 + (self.N - n + 0.5) / (n + 0.5))


def reciprocal_rank_fusion(
    bm25_scores: list[float],
    dense_scores: list[float] | None = None,
    k: int = 60,
    weight_bm25: float = 0.5,
    weight_dense: float = 0.5,
) -> list[float]:
    """Merge BM25 and dense scores using reciprocal rank fusion.

    Args:
        bm25_scores: Per-document BM25 scores.
        dense_scores: Per-document cosine similarity scores (or None for
                      BM25-only mode).
        k: RRF constant (default 60).
        weight_bm25: BM25 weight in the weighted average (default 0.5).
        weight_dense: Dense weight (default 0.5).

    Returns:
        RRF-fused scores, one per document.
    """
    n = len(bm25_scores)

    if dense_scores is None:
        # BM25-only mode — return normalised BM25
        mx = max(bm25_scores) if bm25_scores else 1.0
        return [s / mx for s in bm25_scores] if mx > 0 else bm25_scores

    # Rank-based RRF
    bm25_ranks = _rank_argsort(bm25_scores)
    dense_ranks = _rank_argsort(dense_scores)

    fused = [0.0] * n
    for i in range(n):
        r_bm25 = bm25_ranks[i] + 1  # 1-based
        r_dense = dense_ranks[i] + 1
        fused[i] = weight_bm25 * (1.0 / (k + r_bm25)) + weight_dense * (1.0 / (k + r_dense))

    return fused


def _rank_argsort(scores: list[float]) -> list[int]:
    """Return the rank (0 = highest) of each position."""
    indexed = [(s, i) for i, s in enumerate(scores)]
    indexed.sort(key=lambda x: (-x[0], x[1]))
    ranks = [0] * len(scores)
    for rank, (_, idx) in enumerate(indexed):
        ranks[idx] = rank
    return ranks


def hybrid_search(
    query: str,
    documents: list[str],
    document_ids: list[str] | None = None,
    use_dense: bool = True,
) -> list[dict[str, Any]]:
    """Run hybrid BM25 + dense search over *documents*.

    Args:
        query: The search query string.
        documents: List of document texts to search.
        document_ids: Optional parallel list of identifiers.
        use_dense: Whether to attempt dense embedding scoring (requires
                   fastembed). Falls back to BM25-only if False or if
                   fastembed is unavailable.

    Returns:
        List of result dicts sorted by descending score, each with keys:
        ``id``, ``text``, ``score``, ``bm25_score``, ``dense_score``.
    """
    if document_ids is None:
        document_ids = [str(i) for i in range(len(documents))]

    # BM25 scores
    bm25 = BM25Scorer(documents)
    bm25_scores = bm25.score(query)

    # Dense scores (optional)
    dense_scores: list[float] | None = None
    dense_available = use_dense and has_fastembed()

    if dense_available:
        svc = EmbeddingService.get_instance()
        try:
            query_vec = svc.compute_embedding(query)
            doc_vecs = svc.compute_embeddings_batch(documents)
            dense_scores = [_cosine_similarity(query_vec, dv) for dv in doc_vecs]
        except RuntimeError:
            dense_available = False

    # Fuse
    fused = reciprocal_rank_fusion(bm25_scores, dense_scores)

    # Build results
    results = []
    for i, text in enumerate(documents):
        results.append(
            {
                "id": document_ids[i],
                "text": text,
                "score": fused[i],
                "bm25_score": bm25_scores[i],
                "dense_score": dense_scores[i] if dense_scores else None,
            }
        )

    results.sort(key=lambda x: -x["score"])
    return results


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
