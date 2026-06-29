"""
Hybrid search — BM25 + dense embedding scorer.

Re-exports all symbols from ``helpers.embeddings`` for cleaner imports:

    from helpers.hybrid_search import BM25Scorer, hybrid_search, reciprocal_rank_fusion
"""

from .embeddings import (  # noqa: F401
    BM25Scorer,
    EmbeddingService,
    has_fastembed,
    hybrid_search,
    re_rank_results,
    reciprocal_rank_fusion,
)

__all__ = [
    "BM25Scorer",
    "EmbeddingService",
    "has_fastembed",
    "hybrid_search",
    "re_rank_results",
    "reciprocal_rank_fusion",
]
