from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import networkx as nx

from ...config import settings
from ...helpers.agent_recall import family_member_id, family_members, seed_member_project_id, sync_family_project_ids
from ...helpers.response import fail_obj, ok_obj
from .config import _codegraph_dir, _family_codegraph_dir
from .exclusions import _source_manifest
from .html import _graphify_imports, _html_export
from .io import _load_nx_graph


def _build_family_graph(
    slug: str,
    *,
    include_html: bool = False,
    directed: bool = False,
    out_dir: str | Path | None = None,
    node_limit: int | None = None,
) -> dict[str, Any]:
    """Build the FAMILY graph via graphify's native global-graph mechanism.

    Each member project is built with its OWN root (correct per-project
    ``source_file`` paths — never a synthetic/wrong root), then prefixed with a
    stable member tag via ``prefix_graph_for_global`` and merged into ONE graph
    at ``<config_home>/codegraph/family_<slug>/.ai/codegraph/graph.json``.

    ``out_dir`` (sandbox) redirects ALL output to a custom directory — member
    graphs to ``<out_dir>/members/<tag>/`` and the merged family to
    ``<out_dir>/family/`` — so nothing is written into the member projects
    (ideal for large real-world projects or disposable inspections).

    Correct across drives (D:/frontend + E:/backend): per-project paths stay
    clean; node IDs are tagged ``member::local``; every node carries a ``repo``
    attribute. Cross-project EDGES only form for static coupling graphify can
    see (built per-project in isolation); runtime/API calls belong in memory
    relations, not graph edges.

    Two visualizations: each member's ``graph.html`` stays its OWN per-project
    graph (built with include_html=True), while the combined ``family.html`` is
    generated in the family dir AND mirrored into every member's
    ``.ai/codegraph/family.html`` (or ``<out_dir>/members/<tag>/family.html``
    under a sandbox) so opening ``family.html`` from any member shows the same
    merged graph (to_html embeds the graph data inline, so the copy is
    self-contained).
    """

    from .build import _ensure_readme, _git_head_safe, build_graph

    g = _graphify_imports()
    if g is None:
        return fail_obj(error="graphifyy is not installed")
    node_limit = settings.graph_viz_limit if node_limit is None else node_limit
    render_html = bool(include_html and node_limit != 0)
    # Reconcile declared member project_ids to each project's own .ai/project-id
    # (the project is authoritative) + check for duplicate ids with different paths.
    sync_family_project_ids(slug)
    members = family_members(slug)
    if not members:
        return fail_obj(error=f"family '{slug}' has no declared members")

    import networkx as nx
    from graphify.build import prefix_graph_for_global

    combined_parts: list[nx.Graph] = []
    member_manifests: dict[str, dict[str, Any]] = {}
    member_out_dirs: dict[str, Path] = {}
    sandbox = Path(out_dir) if out_dir is not None else None
    for member in members:
        # Stable member identity (.ai/project-id authoritative > declared > derived),
        # used as the repo:: tag. Seed the marker for fresh projects (identity only).
        tag = family_member_id(slug, member)
        seed_member_project_id(slug, member)
        if sandbox is not None:
            m_out = sandbox / "members" / tag
            res = build_graph(member, include_html=True, out_dir=m_out, node_limit=node_limit)
            graph_json = m_out / "graph.json"
            member_out_dirs[tag] = m_out
        else:
            res = build_graph(member, include_html=True, node_limit=node_limit)
            graph_json = _codegraph_dir(Path(member)) / "graph.json"
            member_out_dirs[tag] = _codegraph_dir(Path(member))
        if not res.get("success"):
            return fail_obj(error=f"family member graph build failed ({tag}): {res.get('error')}")
        if not graph_json.is_file():
            return fail_obj(error=f"family member graph missing for '{tag}'")
        combined_parts.append(prefix_graph_for_global(_load_nx_graph(graph_json), tag))
        member_manifests[tag] = {
            "member": str(Path(member).resolve()),
            "source_manifest": _source_manifest(Path(member)),
        }

    combined = nx.compose_all(combined_parts)
    communities = g["cluster"](combined)
    if sandbox is not None:
        fam_out = sandbox / "family"
    else:
        fam_out = _codegraph_dir(_family_codegraph_dir(slug))
    fam_out.mkdir(parents=True, exist_ok=True)
    graph_path = fam_out / "graph.json"
    g["to_json"](
        combined,
        communities,
        str(graph_path),
        force=True,
        built_at_commit=_git_head_safe(fam_out),
    )
    artifacts = {"graph.json": str(graph_path), "family.html": ""}
    html = None
    if render_html:
        fam_html = fam_out / "family.html"
        # Same enhanced filter/drill-down layer as per-project graph.html.
        html = _html_export(combined, communities, fam_html, node_limit)
        if html in ("full", "aggregated"):
            artifacts["family.html"] = str(fam_html)
            # Mirror the merged visualization into every member location (project
            # .ai/codegraph, or the sandbox member dir) so opening family.html
            # from any member shows the SAME combined graph.
            for tag, member_out in member_out_dirs.items():
                member_out.mkdir(parents=True, exist_ok=True)
                shutil.copy2(fam_html, member_out / "family.html")
    built_at = datetime.now(timezone.utc).isoformat()
    (fam_out / ".build_state.json").write_text(
        json.dumps(
            {
                "built_at": built_at,
                "family": slug,
                "members": {t: m["member"] for t, m in member_manifests.items()},
                "source_manifest": {t: m["source_manifest"] for t, m in member_manifests.items()},
                "nodes": combined.number_of_nodes(),
                "edges": combined.number_of_edges(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    _ensure_readme(fam_out)
    return ok_obj(
        out_dir=str(fam_out),
        artifacts=artifacts,
        nodes=combined.number_of_nodes(),
        edges=combined.number_of_edges(),
        built_at=built_at,
        members=len(members),
        family_html_mirrored_to_members=bool(artifacts["family.html"]),
        node_limit=node_limit,
        html=html,
    )


def _family_status(slug: str) -> dict[str, Any]:
    from .status import graph_status
    """Family graph status: exists + fresh (every member's per-project graph fresh)."""
    out_dir = _codegraph_dir(_family_codegraph_dir(slug))
    state_path = out_dir / ".build_state.json"
    if not state_path.is_file() or not (out_dir / "graph.json").is_file():
        return ok_obj(exists=False, fresh=False, error="no family graph built (run graph_build family=<slug>)")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ok_obj(exists=False, fresh=False, error="corrupt family build state")
    members = family_members(slug)
    fresh = bool(members) and all(bool(graph_status(m).get("fresh")) for m in members)
    return ok_obj(
        exists=True,
        fresh=fresh,
        built_at=state.get("built_at"),
        nodes=state.get("nodes"),
        edges=state.get("edges"),
        members=list((state.get("members") or {}).keys()),
    )
