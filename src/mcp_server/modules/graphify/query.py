from __future__ import annotations

import hashlib
import json
import os
import re
from collections import deque
from pathlib import Path
from typing import Any

from ...config import settings
from ...helpers.embeddings import search_index
from ...helpers.response import fail_obj, ok_obj
from .config import _resolve_graph_root, _resolve_root
from .exclusions import _GLOBAL_EXCLUSIONS, _gitignore_exclusions, _gitignored
from .io import _load_graph
from .status import _graph_freshness, _graph_read_pending


# Avoid circular import by doing this inside the function, or if it works top level:
# from .status import graph_status
# from .io import _load_graph
def _find_node_id(data: dict[str, Any], term: str) -> str | None:
    """Resolve a node by id OR label, in order of specificity:

    0. Exact node ``id`` — so ids returned by ``graph_query`` (e.g.
       ``src_stores_auth_useauthstore``) work unchanged in ``graph_path`` /
       ``graph_explain``. This is what makes node identity CONSISTENT across
       query / explain / path.
    1. Exact label match (case-insensitive).
    2. ``name`` field exact match (some exporters split label/name).
    3. Source-file match — a module path (e.g. ``src/stores/auth.js``) resolves
       to its file/module node, so ``graph_path`` works with file paths directly
       (cross-file navigation). Prefers the module node (label == basename); a
       path-style term also matches by SUFFIX so full paths work regardless of
       the build-root prefix (e.g. ``mcp_server/helpers/agent_recall.py`` matches
       ``src/mcp_server/helpers/agent_recall.py``).
    4. Function-name exact match — the term matches a label after stripping a
       trailing ``()``/``(...)`` call signature (e.g. ``graph_status`` matches
       the node labeled ``graph_status()``). Without this, ``graph_path`` could
       resolve ``graph_status`` to an unrelated node whose label merely
       *contains* the term (e.g. a test named ``test_graph_status_...``).
    5. First substring label match (fallback).
    6. Substring id match (last-resort fallback).
    """
    raw = (term or "").strip()
    if not raw:
        return None
    t = raw.lower()
    nodes = data.get("nodes", [])
    for n in nodes:
        if str(n.get("id") or "") == raw:
            return n.get("id")
    for n in nodes:
        if (n.get("label") or "").lower() == t:
            return n.get("id")
    for n in nodes:
        if (n.get("name") or "").lower() == t:
            return n.get("id")
    for n in nodes:
        src = (n.get("source_file") or "").lower()
        if src == t and (n.get("label") or "").lower() == src.rsplit("/", 1)[-1]:
            return n.get("id")
    for n in nodes:
        if (n.get("source_file") or "").lower() == t:
            return n.get("id")
    # Source-file SUFFIX fallback: a path-style term should resolve regardless of
    # the build-root prefix (e.g. "mcp_server/helpers/agent_recall.py" matches a
    # graph whose source_file is "src/mcp_server/helpers/agent_recall.py").
    for n in nodes:
        src = (n.get("source_file") or "").lower()
        if "/" in t and src.endswith(t):
            return n.get("id")
    for n in nodes:
        label = (n.get("label") or "").lower()
        if label.split("(", 1)[0].strip() == t:
            return n.get("id")
    for n in nodes:
        if t in (n.get("label") or "").lower():
            return n.get("id")
    for n in nodes:
        if t in str(n.get("id") or "").lower():
            return n.get("id")
    return None


def _related_memory(
    workspace_path: str | Path,
    term: str,
    source_file: str | None = None,
    project_id: str | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Find memory entities related to a code symbol.

    Searches agent-recall memory by the symbol label and its source file (the
    correlation key: graph node ``source_file`` + label ↔ memory entity).
    Never raises — correlation is a best-effort enrichment, and a memory search
    failure must not break the graph read.
    """
    try:
        from ...helpers.agent_recall import search_nodes

        terms = [t for t in (term, source_file) if t]
        seen: set[str] = set()
        results: list[dict[str, Any]] = []
        for t in terms:
            if not t:
                continue
            for ent in search_nodes(workspace_path=workspace_path, project_id=project_id, query=t, limit=limit):
                name = ent.get("name") or ent.get("entity_name") or ""
                if not name or name in seen:
                    continue
                seen.add(name)
                results.append(
                    {
                        "name": name,
                        "observations": (ent.get("observations") or [])[:5],
                    }
                )
                if len(results) >= limit:
                    return results
        return results
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return []


def _identifier_scan(
    workspace_path: str | Path,
    root: Path,
    term: str,
    limit: int = 10,
    max_bytes: int = 1_000_000,
) -> list[dict[str, Any]]:
    """Text-scan source files for an identifier the graph did not index.

    graphify only indexes file/function/class/component-level labels — computed,
    ref, prop, and local variables are NOT graph nodes. This fallback greps the
    (noise + project-gitignore-excluded) source tree for the term as a whole-word
    identifier and returns file-level hits so ``graph_query`` never returns a
    dead end for a real identifier (e.g. a Vue ``ref``/``computed``).
    Case-sensitive first, case-insensitive as a fallback. Never raises.
    """
    if not term or len(term) < 2:
        return []
    pattern = re.compile(r"\b" + re.escape(term) + r"\b")
    pattern_ic = re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE)
    exclusions = _gitignore_exclusions(root)
    hits: list[dict[str, Any]] = []
    seen: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = str(Path(dirpath).relative_to(root)).replace("\\", "/")
        parent = "" if rel_dir == "." else rel_dir
        active_ex = exclusions if exclusions is not None else _GLOBAL_EXCLUSIONS
        dirnames[:] = [d for d in dirnames if not active_ex.excludes_dir(parent, d) and not d.endswith(".egg-info")]
        for name in filenames:
            if name.endswith((".pyc", ".pyo")):
                continue
            path = Path(dirpath) / name
            if active_ex.excludes_file(parent, name):
                continue
            if exclusions is not None and _gitignored(root, path, exclusions):
                continue
            try:
                rel = str(path.relative_to(root)).replace("\\", "/")
                if path.stat().st_size > max_bytes:
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            m = pattern.search(text)
            if m is None:
                m = pattern_ic.search(text)
            if m is None:
                continue
            if rel in seen:
                continue
            seen.add(rel)
            hits.append(
                {
                    "id": f"id:{rel}",
                    "label": m.group(0),
                    "type": "identifier",
                    "source_file": rel,
                }
            )
            if len(hits) >= limit:
                break
        if len(hits) >= limit:
            break
    return hits


def query_graph(
    workspace_path: str | Path,
    query: str,
    limit: int = 10,
    kind: str | None = None,
    group_by_file: bool = False,
    root: str | Path | None = None,
    project_id: str | None = None,
    family: str | None = None,
) -> dict[str, Any]:
    """Search graph nodes by label / source file / type (case-insensitive, ranked).

    Appends ``related_memory`` (memory entities matching the query term / source
    files) so the agent sees code + memory together (read-time correlation).
    """
    root = _resolve_graph_root(workspace_path, root, family)
    pending = _graph_read_pending(workspace_path, root, family=family)
    if pending is not None:
        return pending
    data = _load_graph(workspace_path, root)
    if data is None:
        return fail_obj(error="no graph built (run graph_build)")
    q = (query or "").strip().lower()
    if not q:
        return fail_obj(error="query required")

    scored: list[tuple[float, dict[str, Any]]] = []

    # LanceDB Semantic Search
    pid = project_id or settings.get_project_id(workspace_path)
    if not pid:
        pid = hashlib.md5(str(workspace_path).encode("utf-8")).hexdigest()
    table_name = f"codegraph_{pid}"
    semantic_results = search_index(table_name, q, limit=limit, workspace_path=workspace_path)
    # Filter out semantic results that are too far away (distance > 0.80)
    semantic_results = [s for s in semantic_results if s.get("_distance", 1.0) < 0.80]
    semantic_map = {str(res.get("id", "")): res for res in semantic_results}

    # If the query contains no spaces and has underscores or uppercase characters,
    # it's highly likely an exact code identifier lookup (e.g. beta_func, MyClass).
    # We restrict pure semantic fuzzy matches for these, as it breaks exact lookups (and tests).
    is_code_identifier = " " not in q and ("_" in q or any(c.isupper() for c in q))

    for n in data.get("nodes", []):
        ntype = (n.get("type") or "").lower()
        if kind and ntype != kind.lower():
            continue

        label = (n.get("label") or "").lower()
        source = (n.get("source_file") or "").lower()
        nid = str(n.get("id"))

        score = 0.0
        if label == q.lower():
            score += 3.0
        elif q.lower() in label:
            score += 2.0
        elif q.lower() in source:
            score += 1.0
        elif q.lower() in ntype:
            score += 0.5

        if nid in semantic_map:
            # Boost score based on semantic similarity
            if is_code_identifier and score == 0:
                # Do not add pure semantic matches for strict code identifiers
                pass
            else:
                dist = semantic_map[nid].get("_distance", 1.0)
                score += max(0.0, (0.80 - dist) * 5)

        if score > 0:
            scored.append((score, n))

    # Fallback to semantic only if no BM25 matches
    if not scored and semantic_results and not is_code_identifier:
        node_map = {str(n.get("id")): n for n in data.get("nodes", [])}
        for s in semantic_results:
            if s.get("id") in node_map:
                scored.append((1.5, node_map[s["id"]]))

    scored.sort(key=lambda x: (-x[0], (x[1].get("label") or "").lower()))

    if group_by_file:
        seen_files = set()
        deduped = []
        for score, n in scored:
            src = n.get("source_file")
            if src:
                if src in seen_files:
                    continue
                seen_files.add(src)
            deduped.append((score, n))
        scored = deduped

    results = [
        {
            "id": n.get("id"),
            "label": n.get("label"),
            "type": n.get("type"),
            "source_file": n.get("source_file"),
            "repo": n.get("repo"),
            "_origin": "ast",
        }
        for _, n in scored[: max(1, int(limit or 10))]
    ]
    related_memory = _related_memory(workspace_path, q, project_id=project_id, limit=5)

    if not results:
        # Identifier fallback: the term exists in source but is not a graph node
        # (computed/ref/prop/local variables). Return whole-word source hits so
        # the query is not a dead end — mode distinguishes these from node hits.
        fallback = _identifier_scan(
            workspace_path, _resolve_root(workspace_path, root), q, limit=max(1, int(limit or 10))
        )
        if fallback:
            for f in fallback:
                f["_origin"] = "identifier"
            return ok_obj(
                count=len(fallback),
                results=fallback,
                mode="identifier",
                note=(
                    "no graph node for this term — returned whole-word identifier "
                    "matches from source (graph indexes file/function/component labels only)"
                ),
                related_memory=related_memory,
                **_graph_freshness(workspace_path, root, family=family),
            )

    return ok_obj(
        count=len(scored),
        results=results,
        mode="node",
        related_memory=related_memory,
        **_graph_freshness(workspace_path, root, family=family),
    )


def _bfs_path(adj: dict[str, list[str]], start: str, target: str) -> list[str] | None:
    """BFS shortest path from ``start`` to ``target`` over adjacency, or None."""
    if start == target:
        return [start]
    frontier = deque([start])
    prev: dict[str, str | None] = {start: None}
    while frontier:
        cur = frontier.popleft()
        if cur == target:
            break
        for nb in adj.get(cur, []):
            if nb not in prev:
                prev[nb] = cur
                frontier.append(nb)
    if target not in prev:
        return None
    ids: list[str] = []
    cur: str | None = target
    while cur is not None:
        ids.append(cur)
        cur = prev[cur]
    ids.reverse()
    return ids


def _module_nodes(data: dict[str, Any]) -> tuple[dict[str, str], set[str]]:
    """Map ``source_file`` -> module node id, plus the set of module node ids.

    graphify's per-file module nodes have ``label`` == basename of their
    ``source_file`` (e.g. ``DashboardPage.vue`` for ``src/pages/DashboardPage.vue``).
    Module-level edges (``imports_from`` / ``imports``) connect these file nodes,
    which is what lets ``graph_path`` fall back to a file-level path when no
    symbol-level path exists (e.g. a component that imports a store).
    """
    file_to_module: dict[str, str] = {}
    module_ids: set[str] = set()
    for n in data.get("nodes", []):
        src = n.get("source_file") or ""
        label = n.get("label") or ""
        if src and label and label == src.rsplit("/", 1)[-1]:
            file_to_module.setdefault(src, n.get("id"))
            module_ids.add(n.get("id"))
    return file_to_module, module_ids


def path_query(
    workspace_path: str | Path,
    a: str | None = None,
    b: str | None = None,
    from_node: str | None = None,
    to_node: str | None = None,
    root: str | Path | None = None,
    family: str | None = None,
) -> dict[str, Any]:
    """Shortest path (BFS) between two nodes — by id OR label, symbol-first.

    Symbol-level BFS first; if no path, falls back to module/file-level BFS
    (cross-file import relationships). Rich failure diagnostics: distinguishes
    "node(s) not found" from "no path found", and on no-path reports both source
    files plus whether a module relationship exists. ``family`` queries the
    family graph spanning correlated member projects.
    """
    root = _resolve_graph_root(workspace_path, root, family)
    pending = _graph_read_pending(workspace_path, root, family=family)
    if pending is not None:
        return pending
    data = _load_graph(workspace_path, root)
    if data is None:
        return fail_obj(error="no graph built (run graph_build)")

    a = a or from_node
    b = b or to_node
    if not a or not b:
        return fail_obj(error="both start ('a' or 'from_node') and end ('b' or 'to_node') nodes required")

    a_id, b_id = _find_node_id(data, a), _find_node_id(data, b)
    if a_id is None or b_id is None:
        missing = [t for t, i in ((a, a_id), (b, b_id)) if i is None]
        return fail_obj(error=f"node(s) not found: {', '.join(missing)}")

    label = {n.get("id"): n.get("label") for n in data.get("nodes", [])}
    node_info = {n.get("id"): n for n in data.get("nodes", [])}

    adj: dict[str, list[str]] = {}
    for link in data.get("links", []):
        s, tgt = link.get("source"), link.get("target")
        adj.setdefault(s, []).append(tgt)
        adj.setdefault(tgt, []).append(s)

    path = _bfs_path(adj, a_id, b_id)
    mode = "symbol"
    if path is None:
        # Module-level fallback: the two symbols live in files that may be
        # connected by an import (e.g. DashboardPage.vue imports the auth store).
        file_to_module, module_ids = _module_nodes(data)
        src_a = (node_info.get(a_id) or {}).get("source_file") or ""
        src_b = (node_info.get(b_id) or {}).get("source_file") or ""
        ma, mb = file_to_module.get(src_a), file_to_module.get(src_b)
        if ma is not None and mb is not None:
            module_adj: dict[str, list[str]] = {}
            for link in data.get("links", []):
                s, tgt = link.get("source"), link.get("target")
                if s in module_ids and tgt in module_ids:
                    module_adj.setdefault(s, []).append(tgt)
                    module_adj.setdefault(tgt, []).append(s)
            module_path = _bfs_path(module_adj, ma, mb)
            if module_path is not None:
                path = module_path
                mode = "module"

    if path is None:
        # Diagnostics: both nodes exist but are disconnected. Report source
        # files + whether ANY module-level relationship exists between them.
        src_a = (node_info.get(a_id) or {}).get("source_file") or "?"
        src_b = (node_info.get(b_id) or {}).get("source_file") or "?"
        return fail_obj(
            error="no path found between the two nodes",
            no_path=True,
            a={"label": label.get(a_id, a), "source_file": src_a},
            b={"label": label.get(b_id, b), "source_file": src_b},
            hint=(f"no symbol path or module-level (imports_from/imports) connection between {src_a} and {src_b}"),
        )

    result: dict[str, Any] = ok_obj(
        path=[label.get(i, i) for i in path],
        hops=max(0, len(path) - 1),
        mode=mode,
        **_graph_freshness(workspace_path, root, family=family),
    )
    if mode == "module":
        result["note"] = "path found at module/file level (cross-file import)"
    return result


def explain_node(
    workspace_path: str | Path,
    node: str,
    limit: int = 30,
    depth: int = 1,
    root: str | Path | None = None,
    project_id: str | None = None,
    family: str | None = None,
) -> dict[str, Any]:
    """Explain a node: its details + direct neighbours with relation types.

    Appends ``related_memory`` (memory entities matching the symbol label and/or
    its source file) so the agent sees code + memory together.
    """
    root = _resolve_graph_root(workspace_path, root, family)
    pending = _graph_read_pending(workspace_path, root, family=family)
    if pending is not None:
        return pending
    data = _load_graph(workspace_path, root)
    if data is None:
        return fail_obj(error="no graph built (run graph_build)")

    node_id = _find_node_id(data, node)
    if node_id is None:
        return fail_obj(error=f"node '{node}' not found")

    target = next((n for n in data.get("nodes", []) if n.get("id") == node_id), None)
    if target is None:
        return fail_obj(error=f"node '{node}' not found")

    neighbours: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    visited_nodes: set[str] = {node_id}
    frontier = [node_id]

    for _ in range(max(1, int(depth or 1))):
        next_frontier = []
        for cur_id in frontier:
            for link in data.get("links", []):
                if link.get("source") == cur_id:
                    nid, rel = link.get("target"), link.get("relation")
                elif link.get("target") == cur_id:
                    nid, rel = link.get("source"), link.get("relation")
                else:
                    continue
                key = (cur_id, nid, rel) if cur_id < nid else (nid, cur_id, rel)
                if key in seen:
                    continue
                seen.add(key)
                neighbours.append({"node": nid, "relation": rel, "depth": _ + 1})
                if nid not in visited_nodes:
                    visited_nodes.add(nid)
                    next_frontier.append(nid)
            if len(neighbours) >= max(1, int(limit or 30)):
                break
        frontier = next_frontier
        if len(neighbours) >= max(1, int(limit or 30)):
            break

    docblock = None
    if target.get("source_file") and target.get("source_location"):
        try:
            loc = target.get("source_location")
            if isinstance(loc, list) and len(loc) >= 1:
                start_row = int(loc[0])
                full_path = Path(_resolve_graph_root(workspace_path, root, family)) / target["source_file"]
                if full_path.exists():
                    lines = full_path.read_text("utf-8").splitlines()
                    if 0 <= start_row < len(lines):
                        comments = []
                        r = start_row - 1
                        while r >= 0:
                            line = lines[r].strip()
                            if not line:
                                r -= 1
                                continue
                            if (
                                any(line.startswith(prefix) for prefix in ("//", "#", "*", "/*", '"""'))
                                or line.endswith("*/")
                                or line.endswith('"""')
                            ):
                                comments.insert(0, line)
                                r -= 1
                            else:
                                break
                        if comments:
                            docblock = "\n".join(comments)
        except Exception:
            pass

    label = {n.get("id"): n.get("label") for n in data.get("nodes", [])}
    ret_node = {
        "id": target.get("id"),
        "label": target.get("label"),
        "type": target.get("type"),
        "source_file": target.get("source_file"),
        "source_location": target.get("source_location"),
    }
    if docblock:
        ret_node["docblock"] = docblock

    return ok_obj(
        node=ret_node,
        neighbours=[
            {"node": label.get(x["node"], x["node"]), "relation": x["relation"], "depth": x["depth"]}
            for x in neighbours
        ],
        related_memory=_related_memory(
            workspace_path,
            target.get("label") or node,
            source_file=target.get("source_file"),
            project_id=project_id,
            limit=5,
        ),
        **_graph_freshness(workspace_path, root, family=family),
    )
