"""
Tests for helpers/embeddings.py and helpers/hybrid_search.py

Covers:
- BM25Scorer: scoring, edge cases
- reciprocal_rank_fusion: fusion logic
- hybrid_search: integration with fallback
- EmbeddingService: singleton pattern (fastembed not required for tests)
"""

from mcp_server.helpers.hybrid_search import (
    BM25Scorer,
    hybrid_search,
    reciprocal_rank_fusion,
)


class TestBM25Scorer:
    def test_basic_scoring(self):
        corpus = ["the cat sat on the mat", "the dog sat on the log", "birds fly"]
        bm25 = BM25Scorer(corpus)
        scores = bm25.score("cat mat")
        assert len(scores) == 3
        # Document 0 ("the cat sat on the mat") should score highest for "cat mat"
        assert scores[0] > scores[1]
        assert scores[0] > scores[2]

    def test_empty_query(self):
        corpus = ["hello world", "foo bar"]
        bm25 = BM25Scorer(corpus)
        scores = bm25.score("")
        assert scores == [0.0, 0.0]

    def test_empty_corpus(self):
        bm25 = BM25Scorer([])
        scores = bm25.score("anything")
        assert scores == []

    def test_single_document(self):
        corpus = ["the quick brown fox"]
        bm25 = BM25Scorer(corpus)
        scores = bm25.score("quick")
        assert len(scores) == 1
        assert scores[0] > 0

    def test_all_terms_match_all_docs(self):
        corpus = ["python programming", "python coding", "java programming"]
        bm25 = BM25Scorer(corpus)
        scores = bm25.score("python")
        assert scores[0] > 0
        assert scores[1] > 0
        # Documents with "python" get higher scores
        assert scores[0] > scores[2]
        assert scores[1] > scores[2]

    def test_no_match(self):
        corpus = ["aaa bbb", "ccc ddd"]
        bm25 = BM25Scorer(corpus)
        scores = bm25.score("zzz")
        assert all(s == 0.0 for s in scores)


class TestReciprocalRankFusion:
    def test_bm25_only_mode(self):
        """When dense_scores is None, returns normalised BM25 scores."""
        scores = reciprocal_rank_fusion([2.0, 4.0, 1.0], dense_scores=None)
        assert len(scores) == 3
        assert scores[0] == 0.5  # 2/4
        assert scores[1] == 1.0  # 4/4
        assert scores[2] == 0.25  # 1/4

    def test_rrf_fusion(self):
        bm25 = [3.0, 1.0, 2.0]
        dense = [0.9, 0.1, 0.5]
        fused = reciprocal_rank_fusion(bm25, dense, k=60)
        assert len(fused) == 3
        # All fused scores should be positive
        assert all(s > 0 for s in fused)

    def test_rrf_empty(self):
        fused = reciprocal_rank_fusion([], [])
        assert fused == []

    def test_identical_scores(self):
        bm25 = [1.0, 1.0, 1.0]
        fused = reciprocal_rank_fusion(bm25, dense_scores=None)
        # All normalised to 1.0
        assert all(s == 1.0 for s in fused)


class TestHybridSearch:
    def test_bm25_only_fallback(self):
        """Without fastembed, hybrid_search should fall back to BM25."""
        docs = ["the cat sat on the mat", "the dog played in the yard", "birds fly high"]
        results = hybrid_search("cat mat", docs, use_dense=False)
        assert len(results) == 3
        # First result should be the most relevant
        assert results[0]["id"] == "0" or results[0]["text"] == docs[0]
        assert results[0]["score"] > results[2]["score"]
        # dense_score should be None in BM25-only mode
        assert results[0]["dense_score"] is None

    def test_empty_documents(self):
        results = hybrid_search("query", [], use_dense=False)
        assert results == []

    def test_empty_query(self):
        docs = ["hello world", "foo bar"]
        results = hybrid_search("", docs, use_dense=False)
        assert len(results) == 2

    def test_custom_ids(self):
        docs = ["python is great", "java is also good"]
        ids = ["py", "java"]
        results = hybrid_search("python", docs, document_ids=ids, use_dense=False)
        assert len(results) == 2
        assert results[0]["id"] == "py"

    def test_single_document(self):
        docs = ["unique content here"]
        results = hybrid_search("unique", docs, use_dense=False)
        assert len(results) == 1
        assert results[0]["score"] == 1.0  # Only doc, max BM25 = itself


class TestReRankResults:
    """Tests for re_rank_results — used by mem_search(use_dense=True)."""

    def test_re_rank_by_relevance(self):
        from mcp_server.helpers.hybrid_search import re_rank_results

        entities = [
            {
                "name": "auth-service",
                "entityType": "service",
                "observations": ["Handles user authentication with JWT tokens"],
            },
            {
                "name": "payment-service",
                "entityType": "service",
                "observations": ["Processes credit card payments via Stripe"],
            },
            {
                "name": "logging-service",
                "entityType": "service",
                "observations": ["Centralized logging with Elasticsearch"],
            },
        ]
        texts = [
            "auth-service Handles user authentication with JWT tokens",
            "payment-service Processes credit card payments via Stripe",
            "logging-service Centralized logging with Elasticsearch",
        ]
        ids = ["auth-service", "payment-service", "logging-service"]

        # Search for auth-related content
        ranked = re_rank_results("user authentication JWT", texts, ids, entities)
        assert len(ranked) == 3
        # auth-service should be ranked first
        assert ranked[0]["name"] == "auth-service"
        assert "score" in ranked[0]
        assert "bm25_score" in ranked[0]
        assert ranked[0]["bm25_score"] > ranked[1]["bm25_score"]

    def test_re_rank_with_multiple_matches(self):
        from mcp_server.helpers.hybrid_search import re_rank_results

        entities = [
            {"name": "user-api", "observations": ["REST API for user management"]},
            {"name": "user-db", "observations": ["PostgreSQL schema for user data"]},
            {"name": "billing-api", "observations": ["REST API for billing"]},
        ]
        texts = [
            "user-api REST API for user management",
            "user-db PostgreSQL schema for user data",
            "billing-api REST API for billing",
        ]
        ids = ["user-api", "user-db", "billing-api"]

        ranked = re_rank_results("user API", texts, ids, entities)
        assert len(ranked) == 3
        # Both user-api and billing-api contain "API" — user-api also has "user"
        assert ranked[0]["name"] == "user-api"
        assert ranked[1]["name"] in ("user-db", "billing-api")

    def test_re_rank_preserves_unmatched_entities(self):
        from mcp_server.helpers.hybrid_search import re_rank_results

        entities = [
            {"name": "matched-entity", "observations": ["contains the search term"]},
            {"name": "unmatched-entity", "observations": ["completely unrelated content"]},
        ]
        texts = [
            "matched-entity contains the search term",
            "unmatched-entity completely unrelated content",
        ]
        ids = ["matched-entity", "unmatched-entity"]

        ranked = re_rank_results("search term", texts, ids, entities)
        assert len(ranked) == 2
        # Both entities preserved even though one doesn't match well
        names = {r["name"] for r in ranked}
        assert names == {"matched-entity", "unmatched-entity"}

    def test_re_rank_empty_input(self):
        from mcp_server.helpers.hybrid_search import re_rank_results

        ranked = re_rank_results("query", [], [], [])
        assert ranked == []

    def test_re_rank_precision_scenario(self):
        """Simulate a real agent scenario: searching for project conventions."""
        from mcp_server.helpers.hybrid_search import re_rank_results

        # Simulate knowledge graph entities an agent would search
        entities = [
            {
                "name": "project-conventions",
                "entityType": "pattern",
                "observations": [
                    "Use pytest for all testing",
                    "Prefer FastAPI over Flask for new services",
                    "Code style: black + isort + ruff",
                ],
            },
            {
                "name": "deploy-pipeline",
                "entityType": "workflow",
                "observations": [
                    "Deploy via GitHub Actions to staging",
                    "Production deploy requires 2 approvals",
                ],
            },
            {
                "name": "db-schema",
                "entityType": "decision",
                "observations": [
                    "PostgreSQL 16 with pgvector for embeddings",
                    "Use Alembic for migrations",
                ],
            },
            {
                "name": "frontend-stack",
                "entityType": "decision",
                "observations": [
                    "React 18 with TypeScript",
                    "Tailwind CSS for styling",
                ],
            },
        ]
        texts = [
            "project-conventions Use pytest for all testing Prefer FastAPI over Flask ...",
            "deploy-pipeline Deploy via GitHub Actions to staging ...",
            "db-schema PostgreSQL 16 with pgvector for embeddings ...",
            "frontend-stack React 18 with TypeScript ...",
        ]
        ids = [e["name"] for e in entities]

        # Agent asks: "what testing framework do we use?"
        ranked = re_rank_results("testing framework pytest", texts, ids, entities)
        assert ranked[0]["name"] == "project-conventions"
        assert ranked[0]["score"] > ranked[1]["score"]

        # Agent asks: "how do we deploy?"
        ranked = re_rank_results("deploy production pipeline", texts, ids, entities)
        assert ranked[0]["name"] == "deploy-pipeline"

        # Agent asks: "what database?"
        ranked = re_rank_results("database postgresql schema", texts, ids, entities)
        assert ranked[0]["name"] == "db-schema"
