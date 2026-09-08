import re
from pathlib import Path
from typing import Any

from .config import _ENABLE_PHP_IMPLEMENTS_ENRICHMENT


def _enrich_vue(graph: Any, workspace_path: Path) -> None:
    """Enrich the graph by scanning Vue <template> blocks for component edges."""
    for node_id in list(graph.nodes):
        if not node_id.endswith(".vue"):
            continue
        try:
            content = (workspace_path / node_id).read_text(encoding="utf-8")
        except OSError:
            continue

        # Basic regex to find components used in template tags
        # e.g., <MyComponent ...>
        for match in re.finditer(r"<([A-Z][a-zA-Z0-9]+)\b", content):
            comp_name = match.group(1)
            # Find a node in the graph that exports this component name
            # This is a simplified matching; real graphify does more.
            # We add a hyperedge.
            for target_id in graph.nodes:
                if target_id.endswith(f"/{comp_name}.vue"):
                    graph.add_edge(node_id, target_id, type="vue_template")


def _enrich_laravel(graph: Any, workspace_path: Path) -> None:
    """Enrich codegraph with route-to-controller and facade mapping."""
    # Simplified Laravel enrichment
    routes_dir = workspace_path / "routes"
    if not routes_dir.exists():
        return

    for route_file in routes_dir.rglob("*.php"):
        route_id = route_file.relative_to(workspace_path).as_posix()
        if route_id not in graph:
            continue

        try:
            content = route_file.read_text("utf-8")
        except OSError:
            continue

        for match in re.finditer(r"\[([A-Za-z0-9_]+Controller)::class", content):
            controller_name = match.group(1)
            for target_id in graph.nodes:
                if target_id.endswith(f"/{controller_name}.php"):
                    graph.add_edge(route_id, target_id, type="laravel_route")


def _enrich_php_implements(graph: Any, workspace_path: Path) -> None:
    """Regex-based enrichment pass for `implements` edges."""
    if not _ENABLE_PHP_IMPLEMENTS_ENRICHMENT:
        return

    for node_id in list(graph.nodes):
        if not node_id.endswith(".php"):
            continue

        try:
            content = (workspace_path / node_id).read_text("utf-8")
        except OSError:
            continue

        for match in re.finditer(r"\bclass\s+[A-Za-z0-9_]+\s+implements\s+([A-Za-z0-9_,\s]+)", content):
            interfaces = match.group(1).split(",")
            for interface in interfaces:
                iname = interface.strip()
                for target_id in graph.nodes:
                    if target_id.endswith(f"/{iname}.php"):
                        graph.add_edge(node_id, target_id, type="php_implements")


_TYPE_ALIAS_RE = re.compile(r"^[A-Z][A-Za-z0-9_]*(Type)$")  # e.g. LoginStateType
_INTERFACE_RE = re.compile(r"^[A-Z][A-Za-z0-9_]*(Props|State|Values|Params)$")  # e.g. RatingSectionProps
_CONST_OBJECT_RE = re.compile(r"^[A-Z][A-Za-z0-9_]*(Status|Enum|Map|List|Config|Options)$")  # e.g. UserStatus


def _clean_inconsistencies(graph: Any) -> None:
    """Post-processing pass to fix structural inconsistencies in the extracted graph.

    Runs right after ``build_from_json`` — at that point graphify has populated
    ``_callable`` but usually leaves ``type`` unset (base ``type`` values like
    ``class``/``function`` are backfilled by the semantic-type pass that follows).
    So alias/interface detection MUST key on ``_callable`` + the node label, NOT
    on ``type == "class"`` (which is not present yet — the historical bug that let
    TS ``type`` aliases / interfaces / PropTypes stay typed as callable classes).
    """
    nodes_to_remove = []

    def _slug(p: str) -> str:
        p = str(p).replace("\\", "/")
        p = re.sub(r"^\./", "", p)
        p = re.sub(r"\.(ts|tsx|js|jsx|mjs|vue|php|py)$", "", p)
        return re.sub(r"[^A-Za-z0-9]", "_", p).lower()

    for node_id, data in list(graph.nodes(data=True)):
        if data is None:
            continue

        src = data.get("source_file")
        if src:
            # 1. & 5. Module-level nodes typed consistently and labeled by full
            # relative path (strip leading "./" / normalize separators first so
            # the slug comparison survives loose source_file spellings).
            src_norm = str(src).replace("\\", "/")
            if str(node_id) == _slug(src_norm):
                data["type"] = "file"
                data["label"] = src_norm

        # 3. Purge comment/rationale nodes (remove_node drops incident edges too).
        if "_rationale_" in str(node_id):
            nodes_to_remove.append(node_id)
            continue

        label = data.get("label", "")
        if not isinstance(label, str) or not label:
            continue

        is_callable = data.get("_callable") is True
        if not label.endswith("()"):  # functions/methods stay function
            if _TYPE_ALIAS_RE.match(label):
                # TS `type X = ...` alias — a type, not a runtime class.
                data["type"] = "type_alias"
                data["_callable"] = False
            elif label == "PropTypes" or _INTERFACE_RE.match(label):
                # TS interface / PropTypes const — not a callable class.
                data["type"] = "interface"
                data["_callable"] = False
            elif not is_callable and _CONST_OBJECT_RE.match(label):
                # Const/enum-like object (e.g. UserStatus) — a symbol, not a class.
                data["type"] = "symbol"
                data["_callable"] = False

        # 4. Clean raw destructured binding labels: "{ getDefaultConfig }" or
        # "{ resolve: metroResolve }" → the bare identifier. Only rewrite when the
        # whole label is a destructuring literal (never mangle other braces).
        if label.startswith("{") and label.endswith("}"):
            inner = label[1:-1].strip()
            cleaned = inner.split(":")[-1].strip() if ":" in inner else inner
            if cleaned:
                data["label"] = cleaned

    for node_id in nodes_to_remove:
        graph.remove_node(node_id)
