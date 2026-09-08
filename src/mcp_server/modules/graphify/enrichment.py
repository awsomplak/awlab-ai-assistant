import re
from pathlib import Path
from typing import Any

from .config import _ENABLE_PHP_IMPLEMENTS_ENRICHMENT


def _enrich_vue(graph: Any, workspace_path: Path) -> None:
    """Enrich the graph by scanning Vue <template> blocks for component edges."""
    for node_id in list(graph.nodes):
        if not node_id.endswith('.vue'):
            continue
        try:
            content = (workspace_path / node_id).read_text(encoding="utf-8")
        except OSError:
            continue

        # Basic regex to find components used in template tags
        # e.g., <MyComponent ...>
        for match in re.finditer(r'<([A-Z][a-zA-Z0-9]+)\b', content):
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

        for match in re.finditer(r'\[([A-Za-z0-9_]+Controller)::class', content):
            controller_name = match.group(1)
            for target_id in graph.nodes:
                if target_id.endswith(f"/{controller_name}.php"):
                    graph.add_edge(route_id, target_id, type="laravel_route")

def _enrich_php_implements(graph: Any, workspace_path: Path) -> None:
    """Regex-based enrichment pass for `implements` edges."""
    if not _ENABLE_PHP_IMPLEMENTS_ENRICHMENT:
        return

    for node_id in list(graph.nodes):
        if not node_id.endswith('.php'):
            continue

        try:
            content = (workspace_path / node_id).read_text("utf-8")
        except OSError:
            continue

        for match in re.finditer(r'\bclass\s+[A-Za-z0-9_]+\s+implements\s+([A-Za-z0-9_,\s]+)', content):
            interfaces = match.group(1).split(',')
            for interface in interfaces:
                iname = interface.strip()
                for target_id in graph.nodes:
                    if target_id.endswith(f"/{iname}.php"):
                        graph.add_edge(node_id, target_id, type="php_implements")
