"""
Vite/JS path-alias import augmentation — regression tests.

graphifyy resolves relative imports and tsconfig/jsconfig ``paths`` aliases but
NOT Vite's ``resolve.alias`` (a common Vue/Nuxt/Vite convention where no
tsconfig exists, e.g. ``'@': './src'`` in vite.config.js). Unresolved alias
specifiers like ``import { useAuthStore } from '@/stores/auth'`` silently
produced no edge, breaking ``graph_path`` for .vue SFCs and any ``@/``-importing
file. The bridge's ``_augment_alias_import_edges`` pass adds the missing
``imports_from``/``imports`` edges.

Covers: object-form + URL-form + array-form aliases; multi-segment keys
(``@pages``); ``~`` aliases; extensionless resolution; fresh-skip self-heal;
and ``graph_path``/``graph_query`` end-to-end on a .vue SFC.
"""

import json
from pathlib import Path

from mcp_server.helpers import graphify_bridge as bridge


def _write_project(root: Path, alias_style: str = "object") -> None:
    """Write a minimal Vue project: vite.config + store + .vue pages + component."""
    if alias_style == "array":
        (root / "vite.config.js").write_text(
            "import { fileURLToPath, URL } from 'node:url'\n"
            "export default defineConfig({\n"
            "  resolve: {\n"
            "    alias: [\n"
            "      { find: '@', replacement: fileURLToPath(new URL('./src', import.meta.url)) },\n"
            "      { find: '@pages', replacement: './src/pages' },\n"
            "      { find: '~', replacement: './src/components' }\n"
            "    ]\n"
            "  }\n"
            "})\n",
            encoding="utf-8",
        )
    else:
        (root / "vite.config.js").write_text(
            "import { fileURLToPath, URL } from 'node:url'\n"
            "export default defineConfig({\n"
            "  resolve: { alias: {\n"
            "    '@': fileURLToPath(new URL('./src', import.meta.url)),\n"
            "    '@pages': './src/pages',\n"
            "    '~': './src/components'\n"
            "  } }\n"
            "})\n",
            encoding="utf-8",
        )
    (root / "src" / "stores").mkdir(parents=True, exist_ok=True)
    (root / "src" / "stores" / "auth.js").write_text(
        "export function useAuthStore() {\n  return { token: 'x' };\n}\n", encoding="utf-8"
    )
    (root / "src" / "pages").mkdir(parents=True, exist_ok=True)
    (root / "src" / "pages" / "DashboardPage.vue").write_text(
        "<template><div>hi</div></template>\n\n"
        "<script setup>\n"
        "import { useAuthStore } from '@/stores/auth';\n"
        "import SettingsPage from '@pages/SettingsPage.vue';\n"
        "import Btn from '~/Button.vue';\n"
        "</script>\n",
        encoding="utf-8",
    )
    (root / "src" / "pages" / "SettingsPage.vue").write_text(
        "<script setup>\nimport { useAuthStore } from '@/stores/auth';\n</script>\n", encoding="utf-8"
    )
    (root / "src" / "components").mkdir(parents=True, exist_ok=True)
    (root / "src" / "components" / "Button.vue").write_text(
        "<script setup>\nconst x = 1;\n</script>\n", encoding="utf-8"
    )


def _alias_links(data: dict) -> list[dict]:
    return [e for e in data.get("links", []) if e.get("alias_resolved")]


# ── _vite_alias_map: config parsing ────────────────────────────────────────


def test_alias_map_object_url_form(tmp_path: Path):
    """Object-form alias with the fileURLToPath(new URL(...)) replacement."""
    (tmp_path / "vite.config.js").write_text(
        "import { fileURLToPath, URL } from 'node:url'\n"
        "export default defineConfig({ resolve: { alias: { '@': "
        "fileURLToPath(new URL('./src', import.meta.url)) } } })\n",
        encoding="utf-8",
    )
    aliases = bridge._vite_alias_map(tmp_path)
    assert aliases == {"@": (tmp_path / "src").resolve()}


def test_alias_map_array_multi_segment(tmp_path: Path):
    """Array-form aliases with multi-segment keys (@, @pages) and ~ prefix."""
    (tmp_path / "vite.config.js").write_text(
        "export default defineConfig({ resolve: { alias: [\n"
        "  { find: '@', replacement: './src' },\n"
        "  { find: '@pages', replacement: './src/pages' },\n"
        "  { find: '~', replacement: './src/components' }\n"
        "] } })\n",
        encoding="utf-8",
    )
    aliases = bridge._vite_alias_map(tmp_path)
    assert aliases["@"] == (tmp_path / "src").resolve()
    assert aliases["@pages"] == (tmp_path / "src" / "pages").resolve()
    assert aliases["~"] == (tmp_path / "src" / "components").resolve()


def test_alias_map_absent_without_config(tmp_path: Path):
    """No vite/nuxt config → no aliases (no-op pass)."""
    assert bridge._vite_alias_map(tmp_path) == {}


# ── End-to-end: build + edges + path ───────────────────────────────────────


def test_vue_alias_imports_produce_edges_and_path(tmp_path: Path):
    """A .vue SFC importing a store via '@' gains edges and a graph_path."""
    _write_project(tmp_path)
    res = bridge.build_graph(str(tmp_path), include_html=False)
    assert res["success"] is True
    assert res.get("alias_edges", 0) >= 4

    data = json.loads((tmp_path / ".ai" / "codegraph" / "graph.json").read_text(encoding="utf-8"))
    links = _alias_links(data)
    relations = {(e["source"], e["target"], e["relation"]) for e in links}

    def has_edge(a: str, b: str, rel: str) -> bool:
        # Undirected graph: node_link_data may serialize either orientation.
        return (a, b, rel) in relations or (b, a, rel) in relations

    # Module edge: DashboardPage ↔ auth store (imports_from).
    assert has_edge("src_pages_dashboardpage", "src_stores_auth", "imports_from")
    # Symbol edge: DashboardPage → useAuthStore symbol.
    assert ("src_pages_dashboardpage", "src_stores_auth_useauthstore", "imports") in relations
    # @pages (multi-segment) edge ↔ SettingsPage; ~ edge ↔ Button.vue.
    assert has_edge("src_pages_dashboardpage", "src_pages_settingspage", "imports_from")
    assert has_edge("src_pages_dashboardpage", "src_components_button", "imports_from")

    # graph_path resolves both directions.
    p = bridge.path_query(str(tmp_path), "useAuthStore", "DashboardPage.vue")
    assert p["success"] is True
    assert p["hops"] >= 1
    p2 = bridge.path_query(str(tmp_path), "DashboardPage.vue", "Button.vue")
    assert p2["success"] is True
    assert p2["hops"] == 1


def test_alias_edges_idempotent_on_rebuild(tmp_path: Path):
    """Rebuilding an unchanged project does not duplicate alias edges."""
    _write_project(tmp_path)
    bridge.build_graph(str(tmp_path), include_html=False)
    r2 = bridge.build_graph(str(tmp_path), include_html=False)
    assert r2["skipped"] is True  # no changes → fresh-skip
    data = json.loads((tmp_path / ".ai" / "codegraph" / "graph.json").read_text(encoding="utf-8"))
    links = _alias_links(data)
    keys = [(e["source"], e["target"], e["relation"]) for e in links]
    assert len(keys) == len(set(keys))  # no duplicates


def test_fresh_skip_self_heals_stale_graph(tmp_path: Path):
    """A graph.json missing alias edges (old build) gains them on a no-op read."""
    _write_project(tmp_path, alias_style="array")
    bridge.build_graph(str(tmp_path), include_html=False)
    graph_path = tmp_path / ".ai" / "codegraph" / "graph.json"

    # Simulate a graph built before the alias pass: strip all alias links.
    data = json.loads(graph_path.read_text(encoding="utf-8"))
    data["links"] = [e for e in data.get("links", []) if not e.get("alias_resolved")]
    graph_path.write_text(json.dumps(data), encoding="utf-8")
    assert not _alias_links(json.loads(graph_path.read_text(encoding="utf-8")))

    # No source change → fresh-skip, but the self-heal pass restores the edges.
    res = bridge.build_graph(str(tmp_path), include_html=False)
    assert res["skipped"] is True
    assert res.get("alias_edges", 0) >= 4
    assert _alias_links(json.loads(graph_path.read_text(encoding="utf-8")))


def test_relative_imports_untouched(tmp_path: Path):
    """Relative imports (graphifyy's domain) are not re-emitted by the pass."""
    _write_project(tmp_path)
    # A store importing another store relatively — graphifyy already emits this edge.
    (tmp_path / "src" / "stores" / "menu.js").write_text(
        "import { useAuthStore } from './auth';\nexport const x = 1;\n", encoding="utf-8"
    )
    bridge.build_graph(str(tmp_path), include_html=False)
    graph_path = tmp_path / ".ai" / "codegraph" / "graph.json"
    data = json.loads(graph_path.read_text(encoding="utf-8"))
    alias = _alias_links(data)
    # The relative menu→auth edge must NOT be flagged as alias-resolved by us
    # (it came from graphifyy), and our pass must not duplicate it.
    assert not any(e["source"] == "src_stores_menu" for e in alias)
