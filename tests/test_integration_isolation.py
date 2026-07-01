"""
Integration test: verify multi-project isolation via the agent_recall bridge.

Simulates two project_ids, writes data under one, and verifies the other
cannot read it.
"""

from pathlib import Path

import pytest

from mcp_server.helpers.agent_recall import (
    create_bridge,
    create_entities,
    search_nodes,
    delete_entities,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def tmp_project_root(tmp_path_factory):
    """Provide a temp dir, chdir into it, and clean up afterward."""
    tmp_cwd = tmp_path_factory.mktemp("agent-memory-test-integration")
    old_cwd = Path.cwd()
    try:
        yield Path(tmp_cwd)
    finally:
        old_cwd


# ---------------------------------------------------------------------------
# Cross Project Classes
# ---------------------------------------------------------------------------

class TestCrossProjectIsolation:
    """
    Verify that data written under one project_id is NOT visible under another.
    """

    def test_cross_project_isolation(self, tmp_project_root):
        """
        Demonstrate that data written under project_id 'alpha' is NOT visible
        when querying under project_id 'beta'.
        """
        # ── Cleanup any stale data ──────────────────────────────────────────
        for pid in ("alpha", "beta"):
            existing = search_nodes(
                workspace_path=tmp_project_root,
                query="isolation_test_entity",
                limit=10,
                project_id=pid
            )
            for entity in existing:
                delete_entities(
                    workspace_path=tmp_project_root,
                    names=[entity["name"]],
                    project_id=pid
                )

        # ── Write entity under 'alpha' ──────────────────────────────────────
        entity_name = "isolation_test_entity"
        result = create_entities(
            workspace_path=tmp_project_root,
            entities=[{"name": entity_name, "entityType": "test", "observations": ["test data"]}],
            project_id="alpha",
        )
        assert result.get("created", 0) >= 1, f"Failed to create entity under 'alpha': {result}"

        # ── Search under 'alpha' — should find it ───────────────────────────
        alpha_results = search_nodes(
            workspace_path=tmp_project_root,
            query=entity_name,
            limit=10,
            project_id="alpha"
        )
        alpha_names = [e["name"] for e in alpha_results]
        assert entity_name in alpha_names, (
            f"Entity should be visible under 'alpha'. Found: {alpha_names}"
        )

        # ── Search under 'beta' — should NOT find it ────────────────────────
        beta_results = search_nodes(
            workspace_path=tmp_project_root,
            query=entity_name,
            limit=10,
            project_id="beta"
        )
        beta_names = [e["name"] for e in beta_results]
        assert entity_name not in beta_names, (
            f"Entity should NOT be visible under 'beta'. Found: {beta_names}"
        )

        # ── Cleanup ─────────────────────────────────────────────────────────
        delete_entities(workspace_path=tmp_project_root, names=[entity_name], project_id="alpha")
        delete_entities(workspace_path=tmp_project_root, names=[entity_name], project_id="beta")
        print("✅ Cross-project isolation verified: 'alpha' data invisible from 'beta'")