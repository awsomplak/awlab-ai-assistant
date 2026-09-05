"""
Tests for Antigravity profile compilation and host rule isolation.
"""

from __future__ import annotations

import json
from pathlib import Path

PROFILES_DIR = Path(__file__).resolve().parent.parent / "dist" / "profiles"


def test_antigravity_profile_directory_structure():
    ag_dir = PROFILES_DIR / "antigravity"
    assert ag_dir.exists(), "dist/profiles/antigravity does not exist"

    # Rules
    rules_dir = ag_dir / "rules"
    assert rules_dir.is_dir()
    rules = list(rules_dir.glob("*.md"))
    assert len(rules) >= 15, f"Expected at least 15 rules, found {len(rules)}"
    assert (rules_dir / "02-plan-artifacts.md").exists()
    assert (rules_dir / "99-baking-capabilities.md").exists()

    # Antigravity-specific protocol injection in 02-plan-artifacts.md
    ag_plan_rule = (rules_dir / "02-plan-artifacts.md").read_text(encoding="utf-8")
    assert "## Antigravity IDE Planning & Walkthrough Protocol" in ag_plan_rule
    assert "Threshold-Gated Planning" in ag_plan_rule
    assert "Walkthrough Persistence" in ag_plan_rule

    # Ensure monolithic GEMINI.md is NOT created (prevents rule duplication in Antigravity)
    gemini_file = ag_dir / "GEMINI.md"
    assert not gemini_file.exists(), "GEMINI.md monolith should not be created for Antigravity"

    # Skills
    skills_dir = ag_dir / "skills"
    assert skills_dir.is_dir()
    skill_dirs = [d for d in skills_dir.iterdir() if d.is_dir()]
    assert len(skill_dirs) >= 5, f"Expected at least 5 skills, found {len(skill_dirs)}"
    for sd in skill_dirs:
        skill_file = sd / "SKILL.md"
        assert skill_file.exists(), f"Missing SKILL.md in {sd}"
        content = skill_file.read_text(encoding="utf-8")
        assert content.startswith("---"), f"SKILL.md in {sd} missing YAML frontmatter"
        assert "name:" in content and "description:" in content

    # MCP config snippet
    mcp_file = ag_dir / "mcp_config.json"
    assert mcp_file.exists()
    mcp_data = json.loads(mcp_file.read_text(encoding="utf-8"))
    assert "mcpServers" in mcp_data
    assert "awlab-ai-assistant" in mcp_data["mcpServers"]

    # Tool instructions
    instr_file = ag_dir / "instructions.md"
    assert instr_file.exists()
    assert "awlab-ai-assistant Best Practices for Antigravity IDE" in instr_file.read_text(encoding="utf-8")

    # Hooks snippet
    hooks_file = ag_dir / "hooks.json"
    assert hooks_file.exists()
    hooks_data = json.loads(hooks_file.read_text(encoding="utf-8"))
    assert "awlab-ai-assistant" in hooks_data
    for ev in ("PreToolUse", "PostToolUse", "PreInvocation", "Stop"):
        assert ev in hooks_data["awlab-ai-assistant"]


def test_compiled_hooks_directory_contains_antigravity():
    hooks_dir = PROFILES_DIR / "hooks"
    ag_hooks = hooks_dir / "antigravity.hooks.json"
    assert ag_hooks.exists()
    data = json.loads(ag_hooks.read_text(encoding="utf-8"))
    assert "awlab-ai-assistant" in data
    assert "PreToolUse" in data["awlab-ai-assistant"]


def test_host_rule_isolation_preserves_clean_base_for_other_agents():
    """Ensure that Cline, Copilot, Claude, and OpenCode profiles do NOT receive Antigravity rules."""
    # Cline
    cline_rule = PROFILES_DIR / "cline" / "rules" / "02-plan-artifacts.md"
    if cline_rule.exists():
        text = cline_rule.read_text(encoding="utf-8")
        assert "Antigravity IDE Planning & Walkthrough Protocol" not in text

    # Copilot
    copilot_rule = PROFILES_DIR / "copilot" / "02-plan-artifacts.instructions.md"
    if copilot_rule.exists():
        text = copilot_rule.read_text(encoding="utf-8")
        assert "Antigravity IDE Planning & Walkthrough Protocol" not in text

    # Claude
    claude_monolith = PROFILES_DIR / "claude" / "CLAUDE.md"
    if claude_monolith.exists():
        text = claude_monolith.read_text(encoding="utf-8")
        assert "Antigravity IDE Planning & Walkthrough Protocol" not in text

    # OpenCode
    opencode_monolith = PROFILES_DIR / "opencode" / "AGENTS.md"
    if opencode_monolith.exists():
        text = opencode_monolith.read_text(encoding="utf-8")
        assert "Antigravity IDE Planning & Walkthrough Protocol" not in text
