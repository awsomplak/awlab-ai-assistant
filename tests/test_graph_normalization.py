"""Code-graph normalization guarantees (plan 4.x / task 7.3).

Locks in:
1. TS ``type`` aliases / interfaces / const objects are no longer typed ``class``
   with ``_callable=true``; real classes stay ``class``.
2. Module-level nodes are uniformly ``file`` with full-relative-path labels.
3. ``_clean_inconsistencies`` purges ``_rationale_*`` comment nodes (dropping their
   incident edges) and cleans raw destructured labels.
4. ``graph_status`` counts reconcile with the node-producing corpus in graph.json
   (git-metadata / non-node files excluded).
"""

import json

import networkx as nx

from mcp_server.modules import registration
from mcp_server.modules.graphify.enrichment import _clean_inconsistencies


def _tools():
    return registration.mcp._tool_manager._tools


async def action_call(action: str, params: dict | None = None) -> dict:
    tool = _tools()["action_call"]
    params = dict(params or {})
    if action == "graph_build":
        params.setdefault("background", False)
    return json.loads(await tool.fn(action=action, params=params))


def _load_nodes(tmp_path):
    g = json.loads((tmp_path / ".ai" / "codegraph" / "graph.json").read_text(encoding="utf-8"))
    nodes = g["nodes"] if "nodes" in g else g.get("graph", {}).get("nodes", [])
    return {n.get("label"): n for n in nodes}


async def test_ts_aliases_not_classes_and_modules_are_files(tmp_path):
    src = tmp_path / "src"
    (src / "models").mkdir(parents=True)
    (src / "models" / "user.ts").write_text(
        "export interface UserDataType { id: number; name: string; }\n"
        "export type LoginStateType = 'idle' | 'loading' | 'error';\n"
        "export const UserStatus = { ACTIVE: 1 } as const;\n"
        "export class UserService { login() { return true; } }\n",
        encoding="utf-8",
    )
    (src / "a.ts").write_text(
        "import { UserService } from './models/user';\nexport function go() { return new UserService().login(); }\n",
        encoding="utf-8",
    )

    r = await action_call("graph_build", {"workspace_path": str(tmp_path)})
    assert r["success"] is True

    by_label = _load_nodes(tmp_path)
    # TS type declarations are NOT callable classes anymore
    assert by_label["LoginStateType"]["type"] == "type_alias"
    assert by_label["LoginStateType"]["_callable"] is False
    assert by_label["UserDataType"]["type"] in ("type_alias", "interface")  # interface named ...Type
    assert by_label["UserStatus"]["type"] == "symbol"
    # a real class stays a callable class
    assert by_label["UserService"]["type"] == "class"
    assert by_label["UserService"]["_callable"] is True
    # module nodes are uniformly `file` with full relative-path labels
    for lbl in ("src/models/user.ts", "src/a.ts"):
        assert by_label[lbl]["type"] == "file"
        assert by_label[lbl]["label"] == lbl


def test_clean_inconsistencies_purges_rationale_and_cleans_labels():
    g = nx.DiGraph()
    g.add_node("metro_config_getdefaultconfig", label="{ getDefaultConfig }", source_file="metro.config.js")
    g.add_node("metro_config_resolve_metroresolve", label="{ resolve: metroResolve }", source_file="metro.config.js")
    g.add_node("src_screens_account_index_rationale_107", type="class", label="NOTE", source_file="src/x.tsx")
    g.add_edge("metro_config_getdefaultconfig", "src_screens_account_index_rationale_107", type="rationale_for")
    g.add_node(
        "src_models_user_userdatatype",
        type="class",
        _callable=True,
        label="UserDataType",
        source_file="src/models/user.ts",
    )

    _clean_inconsistencies(g)

    data = dict(g.nodes(data=True))
    assert "src_screens_account_index_rationale_107" not in data  # purged
    assert g.number_of_edges() == 0  # incident rationale_for edge dropped
    assert data["metro_config_getdefaultconfig"]["label"] == "getDefaultConfig"
    assert data["metro_config_resolve_metroresolve"]["label"] == "metroResolve"
    assert data["src_models_user_userdatatype"]["type"] == "type_alias"
    assert data["src_models_user_userdatatype"]["_callable"] is False


async def test_graph_status_counts_reconcile_with_nodes(tmp_path):
    (tmp_path / "src").mkdir(parents=True)
    (tmp_path / "src" / "a.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    (tmp_path / "src" / "b.py").write_text("def bar():\n    return 2\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# readme", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("", encoding="utf-8")  # never a graph source

    await action_call("graph_build", {"workspace_path": str(tmp_path)})
    st = (await action_call("graph_status", {"workspace_path": str(tmp_path)}))["result"]
    g = json.loads((tmp_path / ".ai" / "codegraph" / "graph.json").read_text(encoding="utf-8"))
    nodes = g["nodes"] if "nodes" in g else g.get("graph", {}).get("nodes", [])
    node_files = {n.get("source_file") for n in nodes if n.get("source_file")}

    assert st["total_files"] == st["supported_files"]
    assert st["supported_files"] == len(node_files)
    assert ".gitignore" not in node_files
