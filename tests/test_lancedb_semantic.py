"""
Test semantic query scoring and consistency with LanceDB.
Demonstrates why dense vector search outperforms exact match (BM25/grep)
for semantic synonyms.
"""

import pytest

from mcp_server.helpers.embeddings import _HAS_FASTEMBED, _HAS_LANCEDB, add_to_index, search_index


@pytest.mark.skipif(not _HAS_LANCEDB or not _HAS_FASTEMBED, reason="lancedb or fastembed missing")
def test_semantic_query_scoring_outperforms_exact_match(tmp_path):
    """
    Validates that LanceDB semantic search can find conceptually similar
    items even when there is zero keyword overlap, proving its superiority
    over exact text matching for semantic queries.
    """
    table_name = f"test_semantic_{tmp_path.name}"

    # Corpus with zero overlapping keywords to the queries
    documents = [
        "def authenticate_user(token: str) -> bool:\n    # verify JWT token\n    pass",
        "def fetch_network_data(url: str):\n    # GET request via HTTP\n    pass",
        "def calculate_trajectory(velocity: float):\n    # physics math\n    pass",
        "def parse_xml_to_dict(xml_string):\n    # format conversion\n    pass",
    ]
    doc_ids = ["doc_auth", "doc_net", "doc_math", "doc_xml"]

    # 1. Add to LanceDB
    add_to_index(table_name, documents, doc_ids)

    # 2. Query 1: "login verification"
    # Neither "login" nor "verification" appear in doc_auth verbatim ("authenticate", "verify" do).
    results_login = search_index(table_name, "login process", limit=2)
    assert len(results_login) > 0
    top_login = results_login[0]
    print("\n--- SEMANTIC SEARCH OUTPUT ---")
    print("Query: 'login process'")
    print(f"Top result ID: {top_login['id']} (Distance: {top_login['_distance']:.4f})")
    assert top_login["id"] == "doc_auth"
    # Distance should be reasonably low (meaning high similarity)
    assert top_login["_distance"] < 1.0

    # 3. Query 2: "download json from api"
    # No exact match for "download", "json", "api" in doc_net ("fetch", "network", "HTTP", "GET")
    results_net = search_index(table_name, "download json from api", limit=2)
    top_net = results_net[0]
    print("\nQuery: 'download json from api'")
    print(f"Top result ID: {top_net['id']} (Distance: {top_net['_distance']:.4f})")
    assert top_net["id"] == "doc_net"
    assert top_net["_distance"] < 1.0

    # 4. Consistency: Same query should return identical deterministic scores
    results_net_again = search_index(table_name, "download json from api", limit=2)
    assert results_net_again[0]["_distance"] == top_net["_distance"]


def test_semantic_ranking_order(tmp_path):
    """
    Validates that semantic scoring correctly ranks results by semantic proximity.
    """
    if not _HAS_LANCEDB or not _HAS_FASTEMBED:
        pytest.skip("lancedb or fastembed missing")

    table_name = f"test_ranking_{tmp_path.name}"

    documents = [
        "def dog_bark(): pass",  # highly related to animal/dog
        "def cat_meow(): pass",  # related to animal
        "def car_drive(): pass",  # unrelated to animal
        "def plane_fly(): pass",  # unrelated to animal
    ]
    doc_ids = ["dog", "cat", "car", "plane"]

    add_to_index(table_name, documents, doc_ids)

    # Query for "puppy"
    results = search_index(table_name, "puppy", limit=4)
    ranked_ids = [r["id"] for r in results]

    print("\nQuery: 'puppy'")
    for i, r in enumerate(results):
        print(f"Rank {i + 1}: {r['id']} (Distance: {r['_distance']:.4f})")

    # "dog" should be closer to "puppy" than "cat"
    assert ranked_ids[0] == "dog"
    assert ranked_ids[1] == "cat"

    # Distance to "dog" should be strictly less (better) than to "cat"
    dist_dog = results[0]["_distance"]
    dist_cat = results[1]["_distance"]
    assert dist_dog < dist_cat

    # "car" and "plane" should have much worse distances
    dist_car = next(r["_distance"] for r in results if r["id"] == "car")
    assert dist_car > dist_dog + 0.1  # Significant margin
