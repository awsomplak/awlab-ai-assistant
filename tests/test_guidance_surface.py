"""Guidance-surface regression (plan 6.x / task 7.4).

Locks in that the graph-first + family directives actually reach an agent through
every authored/generated surface: the action_call tool description, the generated
SKILL.md, the committed skill asset, and the rule files.
"""

from pathlib import Path

from mcp_server.registry import build_skill_md, build_tool_description

ROOT = Path(__file__).resolve().parent.parent


def test_tool_description_surfaces_graph_first_and_family():
    td = build_tool_description()
    assert "graph_query before reading files" in td  # graph-first navigation pointer
    assert "store='family_<slug>'" in td  # family memory pointer


def test_generated_skill_surfaces_graph_first_and_family():
    skill = build_skill_md()
    assert "Codebase navigation — graph first" in skill
    assert "graph_query" in skill
    assert "Cross-cutting: Project Family Memory" in skill


def test_committed_skill_asset_surfaces_navigation():
    skill = (ROOT / "assets" / "skills" / "awlab-ai-assistant" / "SKILL.md").read_text(encoding="utf-8")
    assert "Codebase navigation — graph first" in skill
    assert "## Cross-cutting: Project Family Memory" in skill


def test_rule_files_surface_graph_first_directive():
    scanner = (ROOT / "assets" / "rules" / "06-project-scanner.md").read_text(encoding="utf-8")
    tokens = (ROOT / "assets" / "rules" / "03-token-strategies.md").read_text(encoding="utf-8")
    assert "Graph-First Code Comprehension" in scanner
    assert "Graph-First, High-Fidelity Search" in tokens
