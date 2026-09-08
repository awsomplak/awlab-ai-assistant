#!/usr/bin/env python3
"""
awlab-ai-assistant Development CLI — Single entry point for all project operations.

Usage:
    run.py build [--no-bin] [--no-rules]
    run.py publish [--target=<name>] [--skip-build] [--force] [--uninstall] [--no-bin]
    run.py test [<pytest-args>...]
    run.py compile-rules
    run.py help [<command>]
    run.py --version

Commands:
    build           Build Python package + compile rules/skills → /dist
    publish         Publish /dist to AI assistant locations
    test            Run the test suite
    lint            Run lint & code hygiene (ruff)
    ruff            Run ruff directly with arbitrary arguments
    compile-rules   Compile rules to assistant-specific profiles
    release         Scaffold release notes in CHANGELOG
    help            Show this message or help for a specific command
"""

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Windows consoles default to cp1252, which cannot encode the box-drawing /
# checkmark glyphs used in this CLI. Reconfigure stdout/stderr to UTF-8 so the
# CLI output never crashes with UnicodeEncodeError (e.g. "✓", "→", "═").
# `reconfigure` is a real IOBase method at runtime but is missing from the
# `TextIO` stub in typeshed, hence the targeted ignore.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

# ══════════════════════════════════════════════════════════════════════════
#  Constants
# ══════════════════════════════════════════════════════════════════════════

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
RULES_SRC = ROOT / "assets" / "rules"
SKILLS_SRC = ROOT / "assets" / "skills"
AGENTS_SRC = ROOT / "assets" / "agents"
WORKFLOWS_SRC = ROOT / "assets" / "workflows"
PROFILES_DIR = DIST / "profiles"
PYTHON_SRC = ROOT / "src" / "mcp_server"
TEST_DIR = ROOT / "tests"

RULE_ORDER = [
    "00-meta.md",
    "05-environment.md",
    "02-plan-artifacts.md",
    "01-memory-bank.md",
    "03-token-strategies.md",
    "06-project-scanner.md",
    "07-model-router.md",
    "04-commands.md",
    "08-project-id.md",
    "09-user-patterns.md",
    "10-pattern-lifecycle.md",
    "11-agent-memory-isolation.md",
    "12-agent-mcp-workspace-path.md",
    "13-file-hygiene.md",
    "14-mcp-offline-cache.md",
]

BIN_EXT = ".exe" if sys.platform.startswith("win") else ""

# ── Executable pair (bridge + worker) ──────────────────────────────────────
# awlab-ai-assistant (BRIDGE_NAME): the stable, host-facing entrypoint every IDE
#   and hook config already references. It is a thin, always-running stdio proxy
#   that keeps the JSON-RPC pipe alive across publishes, and it execs / spawns
#   the worker. Built as a ONE-FILE bundle so it stays a single executable at
#   the exact path below (zero host-config churn).
# awlab-ai-worker (WORKER_NAME): the heavy MCP stdio server, swapped underneath a
#   live bridge during publish. Built ONEDIR like the previous single binary.
BRIDGE_NAME = "awlab-ai-assistant"
WORKER_NAME = "awlab-ai-worker"

# Publish/restart handshake file. The publisher writes it into the shared bin dir
# before stopping workers, and removes it only after BOTH binaries are fully
# written. Every live bridge (and any pending hook exec) waits on this file before
# spawning/exec'ing a worker, so nothing ever runs a half-written binary.
UPDATE_LOCK = ".update_lock"

PUBLISH_BIN_PATH = str(Path.home() / ".awlab-id" / "agent-memory" / "bin" / f"{BRIDGE_NAME}{BIN_EXT}").replace(
    "\\", "/"
)

# Hook hosts MUST call the production PUBLISHED binary (`<bridge> hook ...`) — never
# the build machine's venv/source path (`python -m mcp_server hook`). Generated hook
# configs (hermes __init__.py, hooks/*.json, claude settings, ...) are published to
# machines where the source checkout does not exist, so a baked source path breaks the
# host (Hermes plugin/hook can't run). The bridge os.execv's the worker with the same
# argv (see src/mcp_server/bridge.py), so HOOK_CMD stays put across publishes.
# Local dev can point AWLAB_HOOK_BIN at a specific binary path to override.
# NOTE: HOOK_CMD is the bare command (no surrounding quotes) — each consumer wraps it
# in its own quotes (`command="..."`), so quoting the path here would produce invalid
# output (e.g. a Python plugin `command=""..."" `). Paths with spaces are an
# acceptable trade-off to keep every generated artifact syntactically valid.
HOOK_CMD = f'{os.environ.get("AWLAB_HOOK_BIN") or PUBLISH_BIN_PATH} hook'

PUBLISH_MAP = {
    "binary": (
        "Core Binary (bridge + worker)",
        [
            # awlab-ai-assistant = ONE-FILE bridge (single executable, the stable
            # host entrypoint). awlab-ai-worker = ONEDIR heavy MCP server, flattened
            # into the same bin dir so its exe sits beside the bridge. Publishing is
            # handled by _publish_binary_target() (lock -> stop workers -> swap).
            ("bin/awlab-ai-assistant", "{home}/.awlab-id/agent-memory/bin/"),
            ("bin/awlab-ai-worker", "{home}/.awlab-id/agent-memory/bin/"),
        ],
    ),
    "cline": (
        "Cline",
        [
            ("profiles/cline/rules", "{home}/Documents/Cline/Rules/"),
            ("profiles/cline/skills", "{home}/.agents/skills"),
            # Cline keeps its native destination AND mirrors to the shared MCP data dir
            ("workflows", "{home}/Documents/Cline/Workflows/"),
            ("workflows", "{home}/.awlab-id/agent-memory/work-flows/"),
        ],
    ),
    "copilot": (
        "Copilot",
        [
            ("profiles/copilot", "{home}/.copilot/instructions"),
            ("profiles/copilot/agents", "{home}/.copilot/agents"),
            ("profiles/cline/skills", "{home}/.agents/skills"),
            ("workflows", "{home}/.awlab-id/agent-memory/work-flows/"),
        ],
    ),
    "claude": (
        "Claude",
        [
            ("profiles/claude/CLAUDE.md", "{home}/.claude/CLAUDE.md"),
            ("profiles/claude/skills", "{home}/.claude/skills"),
            ("profiles/claude/agents", "{home}/.claude/agents"),
            ("profiles/hooks/claude.hooks.json", "{home}/.claude/awlab-hooks.json"),
            ("workflows", "{home}/.awlab-id/agent-memory/work-flows/"),
        ],
    ),
    "hermes": (
        "Hermes",
        [
            ("profiles/hermes", "{home}/.hermes/plugins/awlab-ai-assistant"),
            ("workflows", "{home}/.awlab-id/agent-memory/work-flows/"),
        ],
    ),
    "opencode": (
        "OpenCode",
        [
            ("profiles/opencode/AGENTS.md", "{home}/.config/opencode/AGENTS.md"),
            ("profiles/opencode/skills", "{home}/.config/opencode/skills"),
            ("profiles/opencode/opencode.mcp.json", "{home}/.config/opencode/awlab-mcp.json"),
            ("workflows", "{home}/.awlab-id/agent-memory/work-flows/"),
        ],
    ),
    "antigravity": (
        "Google Antigravity & Antigravity IDE",
        [
            ("profiles/antigravity/rules", "{home}/.gemini/config/rules"),
            ("profiles/antigravity/skills", "{home}/.gemini/config/skills"),
            ("profiles/antigravity/mcp_config.json", "{home}/.gemini/config/mcp_config.json"),
            (
                "profiles/antigravity/instructions.md",
                "{home}/.gemini/antigravity-ide/mcp/awlab-ai-assistant/instructions.md",
            ),
            ("profiles/hooks/antigravity.hooks.json", "{home}/.gemini/config/hooks.json"),
            ("workflows", "{home}/.awlab-id/agent-memory/work-flows/"),
        ],
    ),
}

# Static fallback used only when mcp_server is not importable (never drifts in
# practice — the REGISTRY is the source of truth, see _mcp_tools_text()).
_MCP_TOOLS_FALLBACK = """
    ── awlab-ai-assistant (bridge + worker — Windows/Linux/macOS) ──
    Exposes exactly 2 tools on the MCP surface:

        action_call(action, params)   route any REGISTRY action (task_*, plan_*,
                                        mem_*, graph_*, ctx_*, util_*, wf)
        action_help(action)           per-action usage / general help

    The full action surface (20 actions) is driven by src/mcp_server/registry.py
    — a single source of truth (awlab-ai-assistant = thin bridge; the heavy
    server runs as awlab-ai-worker).
"""


def _mcp_tools_text() -> str:
    """Generate the MCP-tools header from the REGISTRY (no drift).

    Reuses ``build_help`` from src/mcp_server/registry.py so the header always
    lists the exact 20-action surface. Falls back to a static string only if
    the package is not importable (e.g. before ``pip install -e .``).
    """
    try:
        from mcp_server.registry import build_help

        overview = build_help()  # grouped list of every action
        return (
            "\n  ── awlab-ai-assistant (bridge + worker — Windows/Linux/macOS) ──\n"
            "    Exposes exactly 2 tools on the MCP surface:\n\n"
            "      action_call(action, params)   route any REGISTRY action\n"
            "      action_help(action)           per-action usage / general help\n\n"
            "    Full action surface (generated from src/mcp_server/registry.py):\n\n"
            + "\n".join("    " + line if line else "" for line in overview.splitlines())
            + "\n"
        )
    except ImportError:
        return _MCP_TOOLS_FALLBACK


MCP_TOOLS = _mcp_tools_text()


# ══════════════════════════════════════════════════════════════════════════
#  Terminal Helpers
# ══════════════════════════════════════════════════════════════════════════


class Style:
    BOLD = "\033[1m"
    DIM = "\033[2m"
    GREEN = "\033[32m"
    CYAN = "\033[36m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    RESET = "\033[0m"
    CHECK = "\u2713"
    CROSS = "\u2717"


def _info(msg: str) -> None:
    print(f"  {Style.CYAN}{msg}{Style.RESET}")


def _ok(msg: str) -> None:
    print(f"  {Style.GREEN}{Style.CHECK} {msg}{Style.RESET}")


def _warn(msg: str) -> None:
    print(f"  {Style.YELLOW}{Style.CROSS} {msg}{Style.RESET}")


def _fail(msg: str, code: int = 1) -> None:
    print(f"  {Style.RED}{Style.CROSS} {msg}{Style.RESET}", file=sys.stderr)
    sys.exit(code)


def _detail(msg: str) -> None:
    print(f"    {Style.DIM}{msg}{Style.RESET}")


def _header(title: str) -> None:
    gap = 60 - len(title) - 4
    print(f"\n  {Style.BOLD}{title}{Style.RESET}")
    print(f"  {'-' * max(gap, 4)}{Style.RESET}")


# ══════════════════════════════════════════════════════════════════════════
#  Help
# ══════════════════════════════════════════════════════════════════════════


def cmd_help(command: str | None = None) -> None:
    texts = {
        "build": """\
Usage: run.py build [options]

Build the project to /dist. A full build cleans /dist first; partial builds
(--no-bin / --no-rules) clean only the outputs they regenerate and leave the
other artifacts intact.

Options:
    --no-bin            Skip Python package build (rules/skills only → dist/profiles)
    --no-rules          Skip rules/skills compilation (binary only → dist/bin)
    --target-os=OS     Target OS: auto (default), windows, linux, macos, all

Output:
    dist/
    ├── build-manifest.json
    ├── bin/               # Executable(s) + source fallback (binary build)
    └── profiles/          # Per-agent compiled rules/skills (rules build)

Examples:
    run.py build                        # Full build (cleans dist, rebuilds all)
    run.py build --no-bin               # Profiles only (keeps dist/bin)
    run.py build --no-rules             # Binary only (keeps dist/profiles)
    run.py build --target-os=all        # Build for all OSes (specs for non-host)
    run.py build --target-os=linux      # Build spec for Linux
""",
        "publish": """\
Usage: run.py publish [options]

Publish /dist contents to AI assistant locations.
Use --uninstall to remove previously installed files.

Options:
    --target=<name>   One of: binary, cline, copilot, claude, hermes, opencode, antigravity, all
    --skip-build      Fail if /dist doesn't exist instead of building
    --force           Skip confirmation prompts
    --uninstall       Remove installed files instead of installing
    --no-bin          Skip publishing binary (when target=all)

Target Paths:
    Binary:
        binary       ~/.awlab-id/agent-memory/bin/awlab-ai-assistant

    Skills:
        cline        ~/.agents/skills/
        copilot      ~/.agents/skills/ (shared with Cline)
        claude       ~/.claude/skills/
        hermes       ~/.hermes/skills/
        opencode     ~/.config/opencode/skills/
        antigravity  ~/.gemini/config/skills/

    Rules:
        cline        ~/Documents/Cline/Rules/
        copilot      ~/.copilot/instructions/
        claude       ~/.claude/CLAUDE.md
        hermes       ~/.hermes/skills/
        opencode     ~/.config/opencode/AGENTS.md
        antigravity  ~/.gemini/config/rules/
""",
        "test": """\
Usage: run.py test [<pytest-args>...]

Run the test suite. Passes all additional arguments to pytest.

Examples:
    run.py test                    # Run all tests
    run.py test -k "test_plan"     # Run tests matching pattern
    run.py test --tb=long          # Verbose traceback
""",
        "ruff": """\
Usage: run.py ruff [<ruff-args>...]

Run ruff directly. Passes all additional arguments to ruff.

Examples:
    run.py ruff check              # Run ruff check
    run.py ruff format             # Run ruff format
    run.py ruff check --fix        # Run ruff check and fix
""",
        "compile-rules": """\
Usage: run.py compile-rules

Compile rules from assets/rules/ into assistant-specific profiles
under dist/profiles/.

Output:
    dist/profiles/
    ├── claude/              (global skills and CLAUDE.md monolith)
    ├── cline/               (global skills and rules for cline)
    ├── copilot/             (global skills and rules for copilot)
    ├── hermes/              (global skills and rules for hermes)
    ├── opencode/            (global AGENTS.md rules + skills)
    ├── antigravity/         (modular rules, skills, mcp, hooks)
    └── .clinerules          (Cline per project rules ready to copy)
""",
    }

    if command and command in texts:
        print(f"\n  {Style.BOLD}run.py {command}{Style.RESET}")
        print(f"  {'=' * (len(command) + 7)}\n{texts[command]}")
        return

    print(__doc__)
    print(f"  Version: {_get_version()} ({_get_build_tag()})")


# ══════════════════════════════════════════════════════════════════════════
#  Compile Rules
# ══════════════════════════════════════════════════════════════════════════


def _load_rules() -> list[dict]:
    """Load raw rule content, preserving HTML comments."""
    rules = []
    for name in RULE_ORDER:
        path = RULES_SRC / name
        if not path.exists():
            _warn(f"Rule file not found: {name}")
            continue
        raw = path.read_text("utf-8")
        rules.append({"filename": name, "content": raw.strip()})
    return rules


def _strip_html_comments(text: str) -> str:
    """Remove HTML comments (``<!-- ... -->``) from rule content."""
    return re.sub(r"<!--[\s\S]*?-->", "", text).strip()


def _offset_headings(text: str, levels: int = 1) -> str:
    """Offset all markdown headings by N levels (e.g. ``##`` \u2192 ``####``)."""

    def _repl(m: re.Match) -> str:
        return "#" * levels + m.group(0)

    return re.sub(r"^#+", _repl, text, flags=re.MULTILINE)


def _build_unified(rules: list[dict]) -> str:
    parts = []
    for r in rules:
        parts.append(f"## {r['filename'].replace('.md', '')}\n\n{r['content']}")
    return "\n\n---\n\n".join(parts)


def _load_skills() -> list[dict]:
    skills = []
    if not SKILLS_SRC.exists():
        return skills
    for entry in sorted(SKILLS_SRC.iterdir()):
        if entry.is_dir():
            md = entry / "SKILL.md"
            if md.exists():
                skills.append({"name": entry.name, "content": md.read_text("utf-8").strip()})
    return skills


# ══════════════════════════════════════════════════════════════════════════
#  Link Rewriting — heading anchors for compiled outputs
# ══════════════════════════════════════════════════════════════════════════


def _rewrite_refs(text: str) -> str:
    """Convert file-based rule references to heading anchors.

    Matches references to rule files (``NN-name.md``) used in ``→ see:``,
    ``defined in``, ``per``, ``as defined in`` patterns — rewrites them to
    ``#NN-name`` heading anchors so they work in compiled monoliths.

    Only rewrites references matching the ``\\d{{2}}-<name>.md`` pattern
    (rule files). Leaves project files (tasks.md, plan.md, registry.md,
    environment.md, notes.md) and template files untouched.
    """

    def _replace(m: re.Match) -> str:
        full = m.group(0)
        name = m.group(1)  # captured base name (e.g. "00-meta")
        # Replace only inside backtick or bold markers
        head = full[: m.start(1) - 1] if m.start(1) > 0 else ""
        return f"{head}#{name}"

    # Match `NN-name.md` or **NN-name.md** — only rule-file style names
    text = re.sub(r"`(\d{2}-[\w-]+)\.md`", r"`#\1`", text)
    text = re.sub(r"\*\*(\d{2}-[\w-]+)\.md\*\*", r"**#\1**", text)
    # Also match bare inline references (not in backticks) — but be conservative
    text = re.sub(r"(?<!\w)(\d{2}-[\w-]+)\.md(?!\w)", r"#\1", text)
    return text


# ══════════════════════════════════════════════════════════════════════════
#  Per-Agent Compilation Pipeline
# ══════════════════════════════════════════════════════════════════════════


def _copy_skills(skills: list[dict], dest_dir: Path, label: str) -> None:
    """Copy all skills into a per-agent skills directory."""
    skills_dir = dest_dir / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    for s in skills:
        d = skills_dir / s["name"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(s["content"], "utf-8")
    _ok(f"{label}  ({len(skills)} skills)")


def _copy_agents(dest_dir: Path, label: str) -> None:
    """Copy shared agent definitions (awlab-baker.md) into a per-host agents dir.

    The shared `awlab-baker.md` uses the Claude .claude/agents format, which
    VS Code Copilot also reads — so one file serves both Claude Code and Copilot.
    """
    if not AGENTS_SRC.exists():
        return
    agents_dir = dest_dir / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    for f in sorted(AGENTS_SRC.glob("*.md")):
        (agents_dir / f.name).write_text(f.read_text(encoding="utf-8"), "utf-8")
    _ok(f"{label}  ({len(list(AGENTS_SRC.glob('*.md')))} agent file(s))")


def _hook_config_for(agent: str, events: list[str]) -> str:
    """Return a per-host hook-registration snippet pointing at the SAME exe.

    Registration is global; the exe derives the project per-event from the payload
    (Hermes cwd, Claude $CLAUDE_PROJECT_DIR, Copilot workspace URI, Cline task cwd).
    """
    if agent == "hermes":
        lines = ["hooks:"]
        for ev in events:
            lines.append(f"  {ev}:")
            lines.append(f'    - command: "{HOOK_CMD} --agent hermes --event {ev}"')
        return "\n".join(lines) + "\n"
    if agent == "claude":
        # Claude Code settings.json hooks: {event: [{matcher?, hooks: [{type, command}]}]}.
        return (
            json.dumps(
                {
                    "hooks": {
                        ev: [{"hooks": [{"type": "command", "command": f"{HOOK_CMD} --agent claude --event {ev}"}]}]
                        for ev in events
                    }
                },
                indent=2,
            )
            + "\n"
        )
    if agent == "antigravity":
        hook_entries: dict[str, list[dict[str, Any]]] = {}
        for ev in events:
            if ev in ("PreToolUse", "PostToolUse"):
                hook_entries[ev] = [
                    {
                        "matcher": "*",
                        "hooks": [{"type": "command", "command": f"{HOOK_CMD} --agent antigravity --event {ev}"}],
                    }
                ]
            else:
                hook_entries[ev] = [{"type": "command", "command": f"{HOOK_CMD} --agent antigravity --event {ev}"}]
        return json.dumps({"awlab-ai-assistant": hook_entries}, indent=2) + "\n"
    # copilot / cline → registration lives in the host UI/settings; emit a note.
    return (
        f"# {agent} hook registration (via host UI/settings):\n"
        + "\n".join(f"#   {HOOK_CMD} --agent {agent} --event {ev}" for ev in events)
        + "\n"
    )


def _baking_capability_note(agent: str) -> str:
    """Per-host pattern-baking tier note (Phase 7).

    Three tiers, ONE shared store (``.ai/memory-bank/observations.jsonl`` +
    ``baked.json``): every tier bakes identical candidates.
        - subagent  : spawn the shared ``awlab-baker`` subagent to observe → bake → report.
        - hooks     : ``{HOOK_CMD} --agent <host> --event <event>`` captures with zero LLM cost.
        - async/inline : server background bake-scheduler + per-action tick (no LLM).
    """
    if agent in ("copilot", "claude", "hermes"):
        tier = (
            "- **Tier: subagent + hooks** — when the delivery marker shows NEW `pattern_candidates`, "
            "spawn the shared `awlab-baker` subagent (see `docs/en/PATTERN_BAKING_PROTOCOL.md`) to "
            "observe → mine → report a distilled pattern list; register hooks "
            f"(`{HOOK_CMD} --agent {agent} --event ...`) for zero-LLM captures."
        )
    elif agent == "antigravity":
        tier = (
            "- **Tier: hooks + inline (Antigravity IDE)** — lifecycle hooks "
            f"(`{HOOK_CMD} --agent antigravity --event ...`) capture observations with zero LLM cost; "
            "the server background bake-scheduler processes candidates automatically; act on "
            "`pattern_candidates` yourself or via skills."
        )
    else:  # cline / opencode — built-in subagents cannot call MCP
        tier = (
            "- **Tier: async + inline (no MCP subagent)** — the server's background bake-scheduler and "
            "per-action tick bake observations automatically (no LLM); act on `pattern_candidates` "
            "yourself per rules 09/10; register hooks "
            f"(`{HOOK_CMD} --agent {agent} --event ...`) for zero-LLM captures."
        )
    return (
        "## Pattern Baking Capabilities\n\n"
        "- **Shared store**: `.ai/memory-bank/observations.jsonl` + `baked.json` — all tiers write the same store.\n"
        f"{tier}\n"
        "- **Spawn gate**: only act when the delivery marker shows NEW `pattern_candidates` "
        "(tell-once, token-cost control).\n"
    )


def _compile_agents(profiles_dir: Path) -> None:
    """Compile the shared baking agent + per-host hook registration configs."""
    # Shared awlab-baker.md → Copilot + Claude (Claude format, both read it).
    _copy_agents(profiles_dir / "copilot", "copilot/agents/  (shared awlab-baker.md)")
    _copy_agents(profiles_dir / "claude", "claude/agents/  (shared awlab-baker.md)")

    # Per-host hook registration configs (all point at the same exe).
    hooks_dir = profiles_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    (hooks_dir / "claude.hooks.json").write_text(
        _hook_config_for(
            "claude", ["UserPromptSubmit", "PostToolUse", "PreToolUse", "SubagentStop", "Stop", "SessionStart"]
        ),
        "utf-8",
    )
    (hooks_dir / "copilot.hooks.txt").write_text(
        _hook_config_for(
            "copilot", ["user-prompt-submit", "post-tool-use", "session-start", "session-end", "subagent-stop", "stop"]
        ),
        "utf-8",
    )
    (hooks_dir / "cline.hooks.txt").write_text(_hook_config_for("cline", ["NewTask", "PostToolUse", "Stop"]), "utf-8")
    (hooks_dir / "antigravity.hooks.json").write_text(
        _hook_config_for("antigravity", ["PreToolUse", "PostToolUse", "PreInvocation", "Stop"]),
        "utf-8",
    )
    _ok("hooks/  (4 per-host hook-registration configs, all → same exe)")


def _compile_cline(rules: list[dict], skills: list[dict], profiles_dir: Path) -> None:
    """Cline: individual .md files + skills + .clinerules monolith."""
    cline_dir = profiles_dir / "cline"
    (cline_dir / "rules").mkdir(parents=True, exist_ok=True)
    for r in rules:
        # Individual files keep HTML comments, rewrite refs to heading anchors
        content = _rewrite_refs(r["content"])
        (cline_dir / "rules" / r["filename"]).write_text(content, "utf-8")
    _ok(f"cline/rules/  ({len(rules)} individual files, HTML comments preserved, heading anchors)")

    # Skills for Cline
    _copy_skills(skills, cline_dir, "cline/skills/")

    # .clinerules monolith (project-level) \u2014 strip HTML comments + rewrite refs
    stripped = [
        {"filename": r["filename"], "content": _rewrite_refs(_strip_html_comments(r["content"]))} for r in rules
    ]
    unified = _build_unified(stripped)
    (profiles_dir / ".clinerules").write_text(
        f"# Cline Rules \u2014 awlab-ai-assistant\n\n{unified}\n\n"
        f"{_baking_capability_note('cline')}\n\n"
        f"## Available MCP Tools\n{MCP_TOOLS}\n",
        "utf-8",
    )
    _ok(".clinerules  (monolith, HTML comments stripped, heading anchors + baking tier note)")


def _compile_copilot(rules: list[dict], skills: list[dict], profiles_dir: Path) -> None:
    """Copilot: individual .instructions.md with YAML frontmatter, stripped comments, offset headings."""
    copilot_dir = profiles_dir / "copilot"
    copilot_dir.mkdir(parents=True, exist_ok=True)

    descriptions = {
        "00-meta": "Rule priority, conflict resolution, and deep analysis protocol",
        "01-memory-bank": "Knowledge-graph memory operations via awlab-ai-assistant mem_* actions",
        "02-plan-artifacts": "Plan registry, task tracking, phase execution format",
        "03-token-strategies": "Context optimization, token budgets, file loading discipline",
        "04-commands": "Session commands and project scanning reference",
        "05-environment": "Shell detection, PowerShell vs Bash command generation",
        "06-project-scanner": "Framework-aware scanning protocol and MCP delegation",
        "07-model-router": "Task complexity classification and model escalation",
        "08-project-id": "Auto-detection of stable project identifier",
        "09-user-patterns": "Trigger points for capturing user preferences",
        "10-pattern-lifecycle": "Pattern storage, conflict resolution, live detection",
        "11-agent-memory-isolation": "Per-project memory namespaces via AGENT_RECALL_SLUG",
        "12-agent-mcp-workspace-path": "Workspace_path parameter rules for MCP tools",
        "13-file-hygiene": "File & workspace hygiene — scratch/temp files",
        "14-mcp-offline-cache": "Offline cache — queue memory/plan mutations when MCP is down; replay on recovery",
    }
    for r in rules:
        base = r["filename"].replace(".md", "")
        desc = descriptions.get(base, f"awlab-ai-assistant rule: {base}")
        # Strip HTML comments, offset headings, rewrite refs to heading anchors
        cleaned = _offset_headings(_strip_html_comments(r["content"]), levels=1)
        cleaned = _rewrite_refs(cleaned)
        frontmatter = f"---\nname: {base}\ndescription: '{desc}'\n---\n\n"
        (copilot_dir / f"{base}.instructions.md").write_text(frontmatter + cleaned, "utf-8")
    # Pattern-baking capability note (Phase 7) — copilot uses subagent + hooks tier.
    (copilot_dir / "99-baking-capabilities.instructions.md").write_text(
        "---\nname: 99-baking-capabilities\n"
        "description: 'Per-host pattern-baking tier note — subagent + hooks'\n---\n\n"
        + _baking_capability_note("copilot"),
        "utf-8",
    )
    _ok(
        f"copilot/  ({len(rules)} .instructions.md files + baking tier note, comments stripped, headings offset, "
        "heading anchors)"
    )


def _compile_claude(rules: list[dict], skills: list[dict], profiles_dir: Path) -> None:
    """Claude Code: single CLAUDE.md monolith + skills, stripped comments, heading anchors."""
    claude_dir = profiles_dir / "claude"
    claude_dir.mkdir(parents=True, exist_ok=True)

    # Strip HTML comments + rewrite refs before building
    processed = []
    for r in rules:
        cleaned = _strip_html_comments(r["content"])
        processed.append({"filename": r["filename"], "content": _rewrite_refs(cleaned)})
    unified = _build_unified(processed)

    (claude_dir / "CLAUDE.md").write_text(
        f"# Claude Code — awlab-ai-assistant\n\nMCP Tools via agent-memory:\n{MCP_TOOLS}\n\n## Rules\n\n{unified}\n\n"
        f"{_baking_capability_note('claude')}\n",
        "utf-8",
    )
    _ok("claude/CLAUDE.md  (monolith, comments stripped, heading anchors + baking tier note)")

    # Skills for Claude Code
    _copy_skills(skills, claude_dir, "claude/skills/")


def _compile_hermes(rules: list[dict], skills: list[dict], profiles_dir: Path) -> None:
    """Hermes: rules as SKILL.md + all skills inside hermes plugin dir."""
    hermes_plugin_dir = profiles_dir / "hermes"
    hermes_dir = hermes_plugin_dir / "skills"
    hermes_dir.mkdir(parents=True, exist_ok=True)

    # Generate plugin.yaml
    (hermes_plugin_dir / "plugin.yaml").write_text(
        f"name: awlab-ai-assistant\n"
        f'version: "{_get_version()}"\n'
        f"description: awlab-ai-assistant plugin with hooks and skills\n",
        "utf-8",
    )

    # Generate __init__.py
    (hermes_plugin_dir / "__init__.py").write_text(
        '"""awlab-ai-assistant hermes plugin"""\n\n'
        "def register(ctx):\n"
        "    # Hooks registered programmatically\n"
        f'    ctx.add_hook("PreToolUse", "awlab-ai-assistant hook", command="{HOOK_CMD} --agent hermes --event PreToolUse")\n'  # noqa: E501
        f'    ctx.add_hook("PostToolUse", "awlab-ai-assistant hook", command="{HOOK_CMD} --agent hermes --event PostToolUse")\n'  # noqa: E501
        f'    ctx.add_hook("PreInvocation", "awlab-ai-assistant hook", command="{HOOK_CMD} --agent hermes --event PreInvocation")\n'  # noqa: E501
        f'    ctx.add_hook("Stop", "awlab-ai-assistant hook", command="{HOOK_CMD} --agent hermes --event Stop")\n',
        "utf-8",
    )

    # ── Rules as awlab-rules/SKILL.md ──
    # Strip HTML comments + rewrite refs
    processed = []
    for r in rules:
        cleaned = _strip_html_comments(r["content"])
        processed.append({"filename": r["filename"], "content": _rewrite_refs(cleaned)})
    unified = _build_unified(processed)

    rules_skill = hermes_dir / "awlab-rules"
    rules_skill.mkdir(parents=True, exist_ok=True)
    (rules_skill / "SKILL.md").write_text(
        f"---\n"
        f"name: awlab-rules\n"
        f"description: awlab-ai-assistant rules for plan management, cross-session memory, "
        f"project scanning, and AI-assisted development conventions\n"
        f"applyTo: '**/*'\n"
        f"---\n"
        f"\n"
        f"# awlab-ai-assistant Rules\n\n"
        f"MCP Tools:\n{MCP_TOOLS}\n\n{unified}\n\n"
        f"{_baking_capability_note('hermes')}\n",
        "utf-8",
    )
    _ok("hermes/skills/awlab-rules/SKILL.md  (skill-packaged rules, comments stripped, anchors + baking tier note)")

    # ── Copy all existing skills ──
    for s in skills:
        d = hermes_dir / s["name"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(s["content"], "utf-8")
    _ok(f"hermes/skills/  ({len(skills)} skills copied)\n    + hermes plugin metadata (plugin.yaml, __init__.py)")


def _compile_opencode(rules: list[dict], skills: list[dict], profiles_dir: Path) -> None:
    """OpenCode: single global AGENTS.md monolith + skills + MCP wiring snippet.

    OpenCode reads global rules from ``~/.config/opencode/AGENTS.md`` and
    discovers skills at ``~/.config/opencode/skills/<name>/SKILL.md`` (each
    SKILL.md needs ``name`` + ``description`` frontmatter; the name must match
    the folder and ``^[a-z0-9]+(-[a-z0-9]+)*$``). MCP servers are configured
    via the ``mcp`` key in ``opencode.json``.
    """
    opencode_dir = profiles_dir / "opencode"
    opencode_dir.mkdir(parents=True, exist_ok=True)

    # ── Rules → AGENTS.md monolith ──
    processed = []
    for r in rules:
        cleaned = _strip_html_comments(r["content"])
        processed.append({"filename": r["filename"], "content": _rewrite_refs(cleaned)})
    unified = _build_unified(processed)
    (opencode_dir / "AGENTS.md").write_text(
        f"# OpenCode — awlab-ai-assistant\n\n"
        f"Global rules for OpenCode (loaded from ~/.config/opencode/AGENTS.md).\n\n"
        f"MCP Tools via awlab-ai-assistant:\n{MCP_TOOLS}\n\n## Rules\n\n{unified}\n\n"
        f"{_baking_capability_note('opencode')}\n",
        "utf-8",
    )
    _ok("opencode/AGENTS.md  (global rules monolith, comments stripped, heading anchors + baking tier note)")

    # ── Skills → skills/<name>/SKILL.md ──
    skills_dir = opencode_dir / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    for s in skills:
        d = skills_dir / s["name"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(s["content"], "utf-8")
    _ok(f"opencode/skills/  ({len(skills)} skills)")

    # ── MCP wiring snippet (merge the `mcp` key into opencode.json) ──
    (opencode_dir / "opencode.mcp.json").write_text(
        json.dumps(
            {
                "$schema": "https://opencode.ai/config.json",
                "mcp": {
                    "awlab-ai-assistant": {
                        "type": "local",
                        "command": [f"dist/bin/awlab-ai-assistant{BIN_EXT}"],
                        "enabled": True,
                    }
                },
            },
            indent=2,
        )
        + "\n",
        "utf-8",
    )
    _ok("opencode/opencode.mcp.json  (merge the `mcp` key into your opencode.json)")


def _compile_antigravity(rules: list[dict], skills: list[dict], profiles_dir: Path) -> None:
    """Antigravity & Antigravity IDE: modular rules + skills + mcp snippet + instructions + hooks."""
    ag_dir = profiles_dir / "antigravity"
    ag_dir.mkdir(parents=True, exist_ok=True)

    # ── Modular Rules (rules/*.md) with Antigravity-specific planning protocol ──
    rules_dir = ag_dir / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)

    antigravity_planning_section = (
        "\n\n## Antigravity IDE Planning & Walkthrough Protocol\n\n"
        "When running in Antigravity or Antigravity IDE:\n"
        "1. **Threshold-Gated Planning**: Simple tasks, Q&A, and quick fixes execute directly "
        "without generating `.ai/artifacts/{uuid}/` (avoids repo clutter). Only when Antigravity "
        "enters Planning Mode or the user asks for a plan is a durable plan referenced or created.\n"
        "2. **Session UI vs. Durable Persistence**:\n"
        "   - During Planning Mode, write `implementation_plan.md` for the user's interactive "
        "review modal and 'Proceed' approval.\n"
        "   - Mirror/link tasks into `.ai/artifacts/{uuid}/tasks.md` and keep them updated with `task_update`.\n"
        "3. **Walkthrough Persistence**: When execution completes, write `walkthrough.md` for the IDE UI "
        'AND persist it to `.ai/artifacts/{uuid}/walkthrough.md` via `action_call(action="plan_doc", '
        'params={"plan_uuid": "<uuid>", "doc": "walkthrough", "mode": "write", "content": ...})`.\n'
    )

    for r in rules:
        content = _strip_html_comments(r["content"])
        content = _rewrite_refs(content)
        # Inject Antigravity-specific planning rules strictly for Antigravity only
        if r["filename"] == "02-plan-artifacts.md":
            content += antigravity_planning_section
        (rules_dir / r["filename"]).write_text(content, "utf-8")

    (rules_dir / "99-baking-capabilities.md").write_text(_baking_capability_note("antigravity"), "utf-8")
    _ok(f"antigravity/rules/  ({len(rules)} modular rules with Antigravity-scoped planning protocol)")

    # ── Skills → skills/<name>/SKILL.md ──
    skills_dir = ag_dir / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    for s in skills:
        d = skills_dir / s["name"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(s["content"], "utf-8")
    _ok(f"antigravity/skills/  ({len(skills)} skills)")

    # ── MCP wiring snippet (merge into ~/.gemini/config/mcp_config.json) ──
    # PUBLISH_BIN_PATH (module-level) = the BRIDGE path hosts already reference;
    # it stays unchanged by the bridge/worker split.
    (ag_dir / "mcp_config.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "awlab-ai-assistant": {
                        "command": PUBLISH_BIN_PATH,
                        "args": [],
                    }
                }
            },
            indent=2,
        )
        + "\n",
        "utf-8",
    )
    _ok("antigravity/mcp_config.json  (merge into ~/.gemini/config/mcp_config.json)")

    # ── Tool instructions (for ~/.gemini/antigravity-ide/mcp/awlab-ai-assistant/instructions.md) ──
    (ag_dir / "instructions.md").write_text(
        "# awlab-ai-assistant Best Practices for Antigravity IDE\n\n"
        "This MCP server provides 23 actions routed through 2 tools: `action_call` and `action_help`.\n\n"
        "## Key Rules:\n"
        "1. **Always pass `workspace_path` (absolute path)** for plan, task, memory, graph, and context actions.\n"
        '2. **Call `action_help(action="<name>")`** to inspect required parameters before calling '
        "an unfamiliar action.\n"
        "3. **Update tasks immediately**: After completing any task step, call `task_update` with `updates`.\n"
        "4. **Persist walkthrough**: When finishing a plan, call "
        '`plan_doc(doc="walkthrough", mode="write", content=...)`.\n',
        "utf-8",
    )
    _ok("antigravity/instructions.md  (tool guidance for Antigravity IDE)")

    # ── Lifecycle Hooks snippet ──
    (ag_dir / "hooks.json").write_text(
        _hook_config_for("antigravity", ["PreToolUse", "PostToolUse", "PreInvocation", "Stop"]),
        "utf-8",
    )
    _ok("antigravity/hooks.json  (merge into ~/.gemini/config/hooks.json or .agents/hooks.json)")


def cmd_compile_rules() -> tuple[list[dict], list[dict]]:
    _header("Compiling Rules & Skills")
    rules = _load_rules()
    skills = _load_skills()

    PROFILES_DIR.mkdir(parents=True, exist_ok=True)

    # Clean old profiles before recompiling to avoid stale artifacts
    for old in list(PROFILES_DIR.glob("*")):
        if old.is_dir():
            shutil.rmtree(old)
        elif old.is_file():
            old.unlink()

    _compile_cline(rules, skills, PROFILES_DIR)
    _compile_copilot(rules, skills, PROFILES_DIR)
    _compile_claude(rules, skills, PROFILES_DIR)
    _compile_hermes(rules, skills, PROFILES_DIR)
    _compile_opencode(rules, skills, PROFILES_DIR)
    _compile_antigravity(rules, skills, PROFILES_DIR)
    _compile_agents(PROFILES_DIR)

    _detail(f"{len(rules)} rules, {len(skills)} skills")
    return rules, skills


# ══════════════════════════════════════════════════════════════════════════
#  Build
# ══════════════════════════════════════════════════════════════════════════


def _detect_current_os() -> str:
    """Detect the current operating system."""
    sys_platform = platform.system().lower()
    if sys_platform == "windows":
        return "windows"
    if sys_platform == "darwin":
        return "macos"
    return "linux"


def _exe_name(base: str, target_os: str) -> str:
    """Return the executable filename for the given OS."""
    return f"{base}.exe" if target_os == "windows" else base


def _stop_awlab_processes() -> None:
    """Stop running WORKER processes before a binary swap (hot-reload aware).

    Only ``awlab-ai-worker`` is terminated — the bridge (``awlab-ai-assistant``)
    must survive so every IDE keeps its JSON-RPC pipe open; it respawns the fresh
    worker once the publisher clears ``.update_lock``. The singleton hook daemon is
    itself an ``awlab-ai-worker`` process (spawned detached via ``sys.executable
    daemon``), so it is stopped here too and lazily respawned by the next hook call.
    Legacy ``awlab-mcp`` is still killed during migration.
    """
    if sys.platform.startswith("win"):
        for name in (f"{WORKER_NAME}.exe", "awlab-mcp.exe"):
            subprocess.run(["taskkill", "/F", "/IM", name], capture_output=True)
    else:
        # Avoid killing unrelated processes (such as IDE language servers or scripts)
        # that may contain 'awlab-ai-worker' in their command-line arguments. Only
        # terminate processes whose executable name is awlab-ai-worker or awlab-mcp.
        try:
            out = subprocess.check_output(["ps", "-eo", "pid,command"], text=True)
            current_pid = os.getpid()
            for line in out.strip().splitlines()[1:]:
                parts = line.strip().split(None, 1)
                if len(parts) < 2:
                    continue
                pid_str, cmd = parts
                try:
                    pid = int(pid_str)
                except ValueError:
                    continue
                if pid == current_pid:
                    continue
                exe_part = cmd.split()[0]
                exe_name = Path(exe_part).name
                if exe_name == WORKER_NAME or exe_name == "awlab-mcp" or exe_name.startswith("awlab-ai-work"):
                    try:
                        os.kill(pid, 15)  # SIGTERM
                    except OSError:
                        pass
        except Exception:
            for name in (WORKER_NAME, "awlab-mcp"):
                subprocess.run(["pkill", "-x", name[:15]], capture_output=True)


def _bin_dir() -> Path:
    """Published binary directory: ``~/.awlab-id/agent-memory/bin/``."""
    return Path.home() / ".awlab-id" / "agent-memory" / "bin"


def _rmtree_if_exists(path: Path) -> None:
    """Remove a file/dir if present (dirs recursively, symlinks unlinked)."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            path.unlink()
        except OSError:
            pass


def _acquire_update_lock(lock: Path, timeout: float = 20.0) -> None:
    """Create ``.update_lock`` atomically (O_CREAT|O_EXCL); wait if busy (4.1)."""
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(f"pid={os.getpid()} time={datetime.now(timezone.utc).isoformat()}\n")
            _info(f"Update lock acquired: {lock}")
            return
        except FileExistsError:
            if time.monotonic() >= deadline:
                _fail(f"Another publish is in progress ({lock} exists). Retry once it clears.")
            _warn(f"Another publish in progress ({lock}) — waiting...")
            time.sleep(0.5)


def _release_update_lock(lock: Path) -> None:
    """Remove ``.update_lock`` LAST so waiting bridges/hooks wake (4.3)."""
    try:
        lock.unlink()
        _ok(f"Update lock released: {lock}")
    except OSError:
        pass


def _replace_file_atomic(src: Path, dest: Path, attempts: int = 6) -> None:
    """Copy ``src`` over ``dest`` atomically (temp + rename), retrying locks."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".awlab-tmp")
    for attempt in range(attempts):
        try:
            shutil.copy2(src, tmp)
            os.replace(tmp, dest)
            if not sys.platform.startswith("win"):
                try:
                    dest.chmod(dest.stat().st_mode | 0o755)
                except OSError:
                    pass
            return
        except PermissionError as exc:
            if attempt < attempts - 1:
                _detail(f"File locked, retrying ({attempt + 1}/{attempts})...")
                time.sleep(0.5)
            else:
                raise exc


def _publish_worker_onedir(src_folder: Path, dest_dir: Path, attempts: int = 6) -> None:
    """Publish the WORKER onedir bundle by flattening it into ``dest_dir``.

    The onedir folder holds the worker executable + its ``_internal`` dependency
    dir. Stale worker artifacts are removed first so two generations never merge
    stale modules. (The bridge is a onefile with no ``_internal``, so
    ``dest_dir/_internal`` always belongs to the worker.)
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    for stale in (
        dest_dir / f"{WORKER_NAME}{BIN_EXT}",
        dest_dir / WORKER_NAME,
        dest_dir / "_internal",
        dest_dir / f"{WORKER_NAME}{BIN_EXT}.awlab-tmp",
    ):
        _rmtree_if_exists(stale)

    for attempt in range(attempts):
        try:
            for item in src_folder.iterdir():
                if item.name == ".DS_Store":
                    continue
                target = dest_dir / item.name
                if item.is_dir():
                    _rmtree_if_exists(target)
                    shutil.copytree(item, target)
                else:
                    _replace_file_atomic(item, target, attempts=1)
            return
        except PermissionError as exc:
            if attempt < attempts - 1:
                _detail(f"Worker files locked, retrying ({attempt + 1}/{attempts})...")
                time.sleep(0.5)
            else:
                raise exc


def _publish_binary_target(home: Path) -> int:
    """Hot-reload publish of the bridge + worker pair (4.1-4.3).

    Sequence: acquire ``.update_lock`` -> stop only WORKERS -> swap both binaries
    -> release the lock LAST. Waiting bridges/hooks then respawn against the fully
    written pair. The lock is released in ``finally`` so a failed publish can never
    wedge every bridge until the stale-lock TTL.
    """
    lock = _bin_dir() / UPDATE_LOCK
    _acquire_update_lock(lock)
    total = 0
    try:
        _info("Stopping running awlab-worker processes (bridges stay up)...")
        _stop_awlab_processes()
        time.sleep(0.3)

        # 1) Worker (ONEDIR) — flatten so its exe sits beside the bridge.
        worker_src = DIST / "bin" / WORKER_NAME
        if worker_src.is_dir():
            _publish_worker_onedir(worker_src, _bin_dir())
            _ok(f"Worker: {_bin_dir() / f'{WORKER_NAME}{BIN_EXT}'}")
            total += 1
        else:
            _warn(f"{worker_src} not found in /dist — worker not published")

        # 2) Bridge (ONE-FILE) — atomic replace of the stable host entrypoint.
        bridge_src = DIST / "bin" / f"{BRIDGE_NAME}{BIN_EXT}"
        if bridge_src.is_file():
            _replace_file_atomic(bridge_src, _bin_dir() / f"{BRIDGE_NAME}{BIN_EXT}")
            _ok(f"Bridge: {_bin_dir() / f'{BRIDGE_NAME}{BIN_EXT}'}")
            total += 1
        else:
            _warn(f"{bridge_src} not found in /dist — bridge not published")
    finally:
        _release_update_lock(lock)
    return total


def _uninstall_binary_target() -> int:
    """Remove the published bridge + worker (4.4): stop workers, drop artifacts."""
    bin_dir = _bin_dir()
    _info("Stopping running awlab-worker processes for binary uninstall...")
    _stop_awlab_processes()
    time.sleep(0.3)

    removed = 0
    for p in (
        bin_dir / f"{BRIDGE_NAME}{BIN_EXT}",
        bin_dir / f"{WORKER_NAME}{BIN_EXT}",
        bin_dir / WORKER_NAME,
        bin_dir / "_internal",
        bin_dir / UPDATE_LOCK,
    ):
        if p.exists() or p.is_symlink():
            _rmtree_if_exists(p)
            _detail(f"Removed {p}")
            removed += 1
    try:
        if bin_dir.exists() and not any(bin_dir.iterdir()):
            bin_dir.rmdir()
    except OSError:
        pass
    return removed


def cmd_build(no_bin: bool = False, no_rules: bool = False, target_os: str = "auto", verbose: bool = False) -> None:
    _header("Build")

    # ── Resolve target OS ──────────────────────────────────────────────────
    current_os = _detect_current_os()
    if target_os == "auto":
        target_os = current_os
        build_targets = [current_os]
    elif target_os == "all":
        build_targets = ["windows", "linux", "macos"]
    else:
        build_targets = [target_os]

    _info(f"Host OS: {current_os}")
    _info(f"Target(s): {', '.join(build_targets)}")

    # MCP server processes are NOT stopped during build because building outputs
    # to /dist, which does not conflict with the published binary in ~/.awlab-id/...
    # Process stopping is strictly isolated to publishing the binary target.

    # Selectively clean /dist based on what is being built, so partial builds
    # never delete artifacts they aren't regenerating:
    #   full build (bin + rules)      → clean everything
    #   rules/skills only (--no-bin)  → clean profiles/rules/skills, keep bin
    #   binary only (--no-rules)      → clean bin, keep profiles
    try:
        if not no_bin and not no_rules:
            # Full build: clean everything and rebuild from scratch.
            if DIST.exists():
                shutil.rmtree(DIST)
                _ok("Cleaned /dist (full build)")
        else:
            # Partial build: clean only what will be regenerated.
            #   --no-rules (bin only)   → clean bin, keep profiles
            #   --no-bin   (rules only) → clean profiles, keep bin
            targets = ("bin",) if not no_bin else ("profiles", "rules", "skills")
            for sub in targets:
                path = DIST / sub
                if path.exists():
                    shutil.rmtree(path)
                    _detail(f"Cleaned /dist/{sub}")
        DIST.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        _warn("Permission denied while cleaning /dist")
        _warn("A stale awlab-* process is still holding the lock. Stop it (scripts/stop-mcp-servers.ps1) and retry.")
        sys.exit(1)

    # 1. Bump build tag — only when the binary is rebuilt (the tag describes the exe)
    if not no_bin:
        vf = PYTHON_SRC / "_version.py"
        raw = vf.read_text("utf-8")
        m = re.search(r'__build_tag__\s*=\s*"build\.(\d+)"', raw)
        if m:
            new = f"build.{int(m.group(1)) + 1:03d}"
            raw = raw.replace(m.group(0), f'__build_tag__ = "{new}"')
            vf.write_text(raw, "utf-8")
            old_tag = m.group(0).split("=")[1].strip().strip('"')
            _info(f"Tag: {old_tag} -> {new}")

    # 2. Compile rules → dist/profiles/{agent}/
    if not no_rules:
        _info("Compiling assets...")
        rules, skills = cmd_compile_rules()
        _detail("profiles/  (per-agent: cline, copilot, claude, hermes)")
        _ok(f"{len(rules)} rules, {len(skills)} skills")

        # 2b. Copy shared workflows → dist/workflows/ for publish
        if WORKFLOWS_SRC.exists():
            wf_dst = DIST / "workflows"
            wf_dst.mkdir(parents=True, exist_ok=True)
            for f in WORKFLOWS_SRC.iterdir():
                if f.is_file():
                    shutil.copy2(f, wf_dst / f.name)
            _ok(f"{len(list(WORKFLOWS_SRC.iterdir()))} workflow files → dist/workflows/")

    # 3. Python package
    if not no_bin:
        _info("Building Python package...")
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-e", str(ROOT)],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
        )
        if r.returncode:
            _fail(f"pip install failed:\n{r.stderr}")

        py_dist = DIST / "bin"
        py_dist.mkdir(parents=True, exist_ok=True)

        # Resolve PyInstaller: PATH first, then the project venv (Windows Scripts
        # or POSIX bin). Only an existing executable is accepted — a bare Path in
        # an ``or`` chain is always truthy, which previously let a non-existent
        # Windows-style path shadow the real POSIX venv binary on macOS/Linux.
        pe = next(
            (
                c
                for c in (
                    shutil.which("pyinstaller"),
                    shutil.which("pyinstaller.exe"),
                    ROOT / ".venv" / "Scripts" / "pyinstaller.exe",  # Windows venv
                    ROOT / ".venv" / "bin" / "pyinstaller",  # POSIX venv (macOS/Linux)
                )
                if c and Path(c).exists()
            ),
            None,
        )
        has_pyinstaller = bool(pe)

        for target in build_targets:
            if target != current_os:
                _warn(f"Cannot build for {target} from {current_os}. Build on a {target} machine instead.")
                # Generate a .spec stub per binary so the user can build on the target OS
                for spec_name in (BRIDGE_NAME, WORKER_NAME):
                    spec_path = ROOT / f"{spec_name}-{target}.spec"
                    spec_path.write_text(
                        f"# PyInstaller spec for {spec_name} (target: {target})\n"
                        f"# Copy this file to a {target} machine and run:\n"
                        f"#   pyinstaller {spec_name}-{target}.spec\n"
                        f"#\n"
                        f"# Auto-generated by run.py build --target-os={target}\n"
                        f"# (Replace paths if the project root differs)\n",
                        "utf-8",
                    )
                    _detail(f"  Spec: {spec_path.name}")
                continue

            if not has_pyinstaller:
                _warn("PyInstaller not found — copying source instead of building executable")
                for f in PYTHON_SRC.rglob("*"):
                    if f.is_file() and "__pycache__" not in str(f):
                        d = py_dist / f.relative_to(PYTHON_SRC)
                        d.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(f, d)
                continue

            # ── Build the bridge + worker pair ─────────────────────────────
            # Two executables, one stable host-facing entrypoint:
            #   * awlab-ai-worker (the heavy FastMCP stdio server) — ONEDIR, as
            #     the previous single binary was. This is the process that gets
            #     swapped underneath a live bridge during publish. Hidden imports
            #     cover every REGISTRY handler module.
            #   * awlab-ai-assistant (the BRIDGE) — a thin, always-running stdio
            #     proxy that keeps an IDE's JSON-RPC pipe alive across publishes.
            #     Built as a ONE-FILE bundle so it publishes as a single
            #     executable at the exact path every host already references, and
            #     it spawns the worker from a sibling file. It imports only
            #     mcp_server.config + helpers.logger (stdlib), so it stays tiny
            #     and starts fast. Its PyInstaller entry is bridge.py, which uses
            #     absolute imports (relative imports cannot be frozen from a .py
            #     entry script).
            BINARIES = [
                {
                    "name": WORKER_NAME,
                    "entry": "__main__.py",
                    "mode": "onedir",
                    "include_source": True,  # full package source available at runtime
                    "hidden_imports": [
                        "mcp_server",
                        "mcp_server.config",
                        "mcp_server.modules",
                        "mcp_server.modules.lifecycle",
                        "mcp_server.modules.registration",
                        "mcp_server.modules.dispatcher",
                        "mcp_server.registry",
                        "mcp_server.helpers",
                        "mcp_server.helpers.agent_recall",
                        "mcp_server.helpers.hybrid_search",
                        "mcp_server.helpers.embeddings",
                        "mcp_server.tools",
                        "mcp_server.tools.context_tools",
                        "mcp_server.tools.file_tools",
                        "mcp_server.tools.utils_tools",
                        "mcp_server.tools.plan_tools",
                        "mcp_server.tools.memory_tools",
                    ],
                },
                {
                    "name": BRIDGE_NAME,
                    "entry": "bridge.py",
                    "mode": "onefile",
                    "include_source": False,  # bridge only needs config + logger
                    "hidden_imports": [],
                },
            ]

            pkg_src = str(PYTHON_SRC)
            pkg_dest = "mcp_server"
            sep = ";" if target == "windows" else ":"

            for spec in BINARIES:
                bin_name = spec["name"]
                _info(f"Building {bin_name} ({spec['mode']}) for {target}...")
                exe_name = _exe_name(bin_name, target)
                cmd = [
                    str(pe),
                    f"--{spec['mode']}",
                    "--distpath",
                    str(py_dist),
                    "--name",
                    bin_name,
                    "--paths",
                    str(PYTHON_SRC.parent),
                ]
                if spec["include_source"]:
                    cmd.extend(["--add-data", f"{pkg_src}{sep}{pkg_dest}"])
                for hi in spec["hidden_imports"]:
                    cmd.extend(["--hidden-import", hi])
                cmd.append(str(PYTHON_SRC / spec["entry"]))

                if verbose:
                    print(f"  {Style.DIM}(PyInstaller logs will stream below...){Style.RESET}")
                    result = subprocess.run(cmd, cwd=str(ROOT))
                else:
                    print(f"  {Style.DIM}(PyInstaller running silently...){Style.RESET}")
                    result = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
                    if result.returncode != 0:
                        print(f"\n{Style.BRIGHT_RED}PyInstaller Output:{Style.RESET}\n{result.stdout}\n{result.stderr}")

                # Clean PyInstaller .spec file (leave 'build' so subsequent builds take 20s instead of 8m)
                spec_path = ROOT / f"{bin_name}.spec"
                if spec_path.exists():
                    spec_path.unlink()

                if result.returncode == 0:
                    _ok(f"Executable: {py_dist / exe_name}")
                else:
                    _warn(f"PyInstaller ({bin_name}/{target}) failed with code {result.returncode}")

    # Clean up generated .spec files after build (both binary names)
    for spec_name in (BRIDGE_NAME, WORKER_NAME):
        for spec_file in ROOT.glob(f"{spec_name}-*.spec"):
            try:
                spec_file.unlink()
            except OSError:
                pass

    # 4. Manifest (records both built artifacts)
    (DIST / "build-manifest.json").write_text(
        json.dumps(
            {
                "version": _get_version(),
                "buildTag": _get_build_tag(),
                "buildTime": datetime.now(timezone.utc).isoformat(),
                "artifacts": [WORKER_NAME, BRIDGE_NAME],
            },
            indent=2,
        ),
        "utf-8",
    )

    print(f"\n  {Style.GREEN}{Style.BOLD}\u2713 Build complete{Style.RESET}  {Style.DIM}\u2192 {DIST}{Style.RESET}")


# ══════════════════════════════════════════════════════════════════════════
#  Publish
# ══════════════════════════════════════════════════════════════════════════


def _resolve_dest(dest_tpl: str, home: Path, name: str = "") -> Path:
    return Path(dest_tpl.replace("{home}", str(home)).replace("{name}", name).replace("{BIN_EXT}", BIN_EXT))


def cmd_publish(
    target: str = "all",
    skip_build: bool = False,
    force: bool = False,
    uninstall: bool = False,
    no_bin: bool = False,
) -> None:
    """Publish awlab-ai-assistant to target(s)."""
    home = Path.home()
    if target == "all":
        targets = list(PUBLISH_MAP)
    elif target in PUBLISH_MAP:
        targets = [target]
    else:
        _fail(f"Unknown target: '{target}'. Available: {', '.join(list(PUBLISH_MAP) + ['all'])}")
        return

    if no_bin and "binary" in targets:
        targets.remove("binary")

    needs_bin = "binary" in targets
    needs_rules = any(t != "binary" for t in targets)

    if uninstall:
        _header("Uninstalling")

        total = 0
        for t in targets:
            if t not in PUBLISH_MAP:
                continue
            label, mappings = PUBLISH_MAP[t]
            _info(f"Removing: {label}")
            if t == "binary":
                total += _uninstall_binary_target()
                continue
            for _, dest_tpl in mappings:
                if "{name}" in dest_tpl:
                    sd = DIST / "skills"
                    if sd.exists():
                        for sd_entry in sd.iterdir():
                            if not sd_entry.is_dir():
                                continue
                            dest = _resolve_dest(dest_tpl, home, sd_entry.name)
                            if dest.exists():
                                dest.unlink()
                                _detail(f"{dest}")
                                total += 1
                    continue
                dest = _resolve_dest(dest_tpl, home)
                if dest.exists():
                    if dest.is_dir():
                        for f in dest.rglob("*.md"):
                            f.unlink()
                            _detail(f"{f}")
                            total += 1
                        # Remove empty subdirectories
                        for d in sorted(dest.rglob("*"), key=lambda p: str(p), reverse=True):
                            if d.is_dir() and not any(d.iterdir()):
                                d.rmdir()
                    else:
                        dest.unlink()
                        _detail(f"{dest}")
                        total += 1
        print(f"\n  {Style.GREEN}{Style.BOLD}\u2713 Removed {total} file(s){Style.RESET}")
        return

    # Check build prerequisites selectively based on target(s)
    bridge_artifact = DIST / "bin" / f"{BRIDGE_NAME}{BIN_EXT}"
    worker_artifact = DIST / "bin" / WORKER_NAME / f"{WORKER_NAME}{BIN_EXT}"
    bin_ready = bridge_artifact.is_file() and worker_artifact.is_file()
    profiles_dir = DIST / "profiles"

    if not DIST.exists():
        if skip_build:
            _fail("/dist not found. Run build first.")
        if needs_bin and not needs_rules:
            _info("/dist not found — building binary first\n")
            cmd_build(no_rules=True)
        elif needs_rules and not needs_bin:
            _info("/dist not found — compiling rules/skills first\n")
            cmd_build(no_bin=True)
        else:
            _info("/dist not found — building all first\n")
            cmd_build()
    else:
        if needs_bin and not bin_ready:
            if skip_build:
                _fail(f"Binaries not found in {DIST / 'bin'} (bridge + worker). Run build first.")
            _info("Binaries not found in /dist — building binary first\n")
            cmd_build(no_rules=True)
        if needs_rules and not profiles_dir.exists():
            if skip_build:
                _fail(f"Profiles not found in {profiles_dir}. Run compile-rules or build first.")
            _info("Profiles not found in /dist — compiling rules first\n")
            cmd_build(no_bin=True)

    mf = DIST / "build-manifest.json"
    if mf.exists():
        try:
            m = json.loads(mf.read_text("utf-8"))
            _info(f"v{m['version']} ({m['buildTag']}) — built {m['buildTime'][:19]}")
        except Exception:
            pass

    total = 0

    for t in targets:
        if t not in PUBLISH_MAP:
            _warn(f"Unknown target: {t}")
            continue
        label, mappings = PUBLISH_MAP[t]
        _header(f"Publishing: {label}")

        # Binary publish is lock + hot-reload aware (bridge stays alive); handled
        # entirely by _publish_binary_target().
        if t == "binary":
            total += _publish_binary_target(home)
            continue

        for src_rel, dest_tpl in mappings:
            if "{name}" in dest_tpl:
                sd = DIST / "skills"
                if not sd.exists():
                    continue
                for sd_entry in sd.iterdir():
                    if not sd_entry.is_dir():
                        continue
                    src = sd_entry / "SKILL.md"
                    if not src.exists():
                        continue
                    dest = _resolve_dest(dest_tpl, home, sd_entry.name)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest)
                    _detail(f"{dest}")
                    total += 1
                continue

            src = DIST / src_rel
            dest = _resolve_dest(dest_tpl, home)
            if src.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
                for item in src.rglob("*"):
                    if item.is_file():
                        rel = item.relative_to(src)
                        (dest / rel.parent).mkdir(parents=True, exist_ok=True)
                        shutil.copy2(item, dest / rel)
                        _detail(f"{dest / rel}")
                        total += 1
            elif src.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)

                # Check if exists and is json then deep merge
                if dest.exists() and dest.suffix == ".json" and not force:
                    try:
                        with open(src, "r", encoding="utf-8") as fs, open(dest, "r", encoding="utf-8") as fd:
                            src_json = json.load(fs)
                            dest_json = json.load(fd)

                        def deep_merge(d1: dict, d2: dict) -> None:
                            for k, v in d2.items():
                                if k in d1 and isinstance(d1[k], dict) and isinstance(v, dict):
                                    deep_merge(d1[k], v)
                                else:
                                    d1[k] = v

                        deep_merge(dest_json, src_json)
                        with open(dest, "w", encoding="utf-8") as fd:
                            json.dump(dest_json, fd, indent=2)

                        _detail(f"Merged into {dest}")
                        total += 1
                        continue
                    except Exception as e:
                        _warn(f"Failed to merge JSON {dest}: {e}")

                # Fallback to copy
                shutil.copy2(src, dest)
                _detail(f"{dest}")
                total += 1
            else:
                _warn(f"{src_rel} not found")

        # Auto-inject plugin to hermes config.yaml
        if t == "hermes" and not uninstall:
            hermes_config = home / ".hermes" / "config.yaml"
            if hermes_config.exists():
                try:
                    import yaml

                    content = yaml.safe_load(hermes_config.read_text("utf-8")) or {}
                    if "plugins" not in content:
                        content["plugins"] = {}
                    if "enabled" not in content["plugins"]:
                        content["plugins"]["enabled"] = []
                    if "awlab-ai-assistant" not in content["plugins"]["enabled"]:
                        content["plugins"]["enabled"].append("awlab-ai-assistant")
                        with open(hermes_config, "w", encoding="utf-8") as f:
                            yaml.safe_dump(content, f, sort_keys=False, default_flow_style=False)
                        _ok("Auto-injected awlab-ai-assistant to ~/.hermes/config.yaml plugins.enabled")
                except ImportError:
                    # Fallback text injection if pyyaml is not available
                    text = hermes_config.read_text("utf-8")
                    if "awlab-ai-assistant" not in text:
                        _warn("PyYAML not found, attempting text injection into hermes config...")
                        if "plugins:" in text and "enabled:" in text:
                            text = text.replace("enabled:", "enabled:\n  - awlab-ai-assistant")
                            hermes_config.write_text(text, "utf-8")
                            _ok("Text-injected awlab-ai-assistant to ~/.hermes/config.yaml plugins.enabled")
                        else:
                            _warn(
                                "Could not inject hermes plugin automatically. Please add awlab-ai-assistant to plugins.enabled manually."  # noqa: E501
                            )
                except Exception as e:
                    _warn(f"Failed to auto-register hermes plugin: {e}")

    print(f"\n  {Style.GREEN}{Style.BOLD}\u2713 Published {total} file(s){Style.RESET}")


# ══════════════════════════════════════════════════════════════════════════
#  Test
# ══════════════════════════════════════════════════════════════════════════


def cmd_test(pytest_args: list[str] | None = None) -> None:
    _header("Test Suite")
    cmd = [sys.executable, "-m", "pytest", str(TEST_DIR), "-q"]
    if pytest_args:
        cmd.extend(pytest_args)
    r = subprocess.run(cmd, cwd=str(ROOT))
    sys.exit(r.returncode)


def cmd_lint(fix: bool = False, apply_format: bool = False, paths: list[str] | None = None) -> None:
    """Run the lint & code-hygiene script (ruff)."""
    _header("Lint & Code Hygiene")
    cmd = [sys.executable, str(ROOT / "scripts" / "lint.py")]
    if fix:
        cmd.append("--fix")
    if apply_format:
        cmd.append("--format")
    if paths:
        cmd.extend(paths)
    r = subprocess.run(cmd, cwd=str(ROOT))
    sys.exit(r.returncode)


def cmd_ruff(ruff_args: list[str] | None = None) -> None:
    """Run ruff directly with arguments (delegates to lint.py)."""
    cmd = [sys.executable, str(ROOT / "scripts" / "lint.py"), "--ruff"]
    if ruff_args:
        cmd.extend(ruff_args)
    r = subprocess.run(cmd, cwd=str(ROOT))
    sys.exit(r.returncode)


# ══════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════


def _get_version() -> str:
    try:
        from mcp_server._version import __version__

        return __version__
    except ImportError:
        return "0.0.0"


def _get_build_tag() -> str:
    try:
        from mcp_server._version import __build_tag__

        return __build_tag__
    except ImportError:
        return "build.000"


def cmd_release(target_version: str | None = None) -> None:
    version = target_version or _get_version()
    print(f"Scaffolding release notes for v{version}...")

    changelog_path = ROOT / "CHANGELOG.md"
    template_path = ROOT / "assets" / "template" / "release_template.md"

    if not changelog_path.exists():
        print("ERROR: CHANGELOG.md not found.")
        return
    if not template_path.exists():
        print("ERROR: Release template not found in assets/template.")
        return

    changelog_text = changelog_path.read_text("utf-8")
    target_header = f"## [{version}]"

    if target_header not in changelog_text:
        print(f"ERROR: Version block {target_header} not found in CHANGELOG.md!")
        print("Please add the version header (e.g. `## [3.0.6]`) first.")
        return

    # Extract template sections (H2 -> H3 for CHANGELOG nesting)
    template_lines = template_path.read_text("utf-8").splitlines()
    template_content = []
    for line in template_lines:
        if line.startswith("## "):
            template_content.append("#" + line)
        elif line.startswith("<!--") or (not line and template_content):
            template_content.append(line)

    template_str = "\n".join(template_content).strip()

    # Check if we already injected to avoid duplicates
    if "### 🚀 Highlights" in changelog_text[changelog_text.find(target_header) :]:
        print("Release template is already present in this version block.")
        return

    # Inject right under the version header, preserving date updates
    lines = changelog_text.splitlines()
    new_lines = []
    in_version = False
    injected = False
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for line in lines:
        if line.startswith("## [") and target_header in line:
            in_version = True
            if " - " not in line:
                line = f"{target_header} - {today}"
            else:
                line = re.sub(r" - \d{4}-\d{2}-\d{2}", f" - {today}", line)
            new_lines.append(line)
            continue

        if in_version and line.startswith("## ["):
            in_version = False

        if in_version and not injected:
            new_lines.append("")
            new_lines.append(template_str)
            new_lines.append("")
            injected = True

        new_lines.append(line)

    changelog_path.write_text("\n".join(new_lines) + "\n", "utf-8")
    print("SUCCESS: Release template injected into CHANGELOG.md!")


# ══════════════════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════════════════


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run.py",
        description="awlab-ai-assistant Development CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="store_true", help="Show version")

    sub = p.add_subparsers(dest="command")

    for name, opts, desc in [
        ("build", ["--no-bin", "--no-rules", "--target-os", "--verbose"], "Build everything to /dist"),
        (
            "publish",
            ["--target", "--skip-build", "--force", "--uninstall", "--no-bin"],
            "Publish /dist to AI assistants",
        ),
        ("test", ["pytest_args"], "Run test suite"),
        ("lint", ["--fix", "--format", "paths"], "Run lint & code hygiene (ruff)"),
        ("ruff", ["ruff_args"], "Run ruff directly with arguments"),
        ("compile-rules", [], "Compile rules to assistant profiles"),
        ("release", ["--target-version"], "Scaffold release notes in CHANGELOG"),
        ("help", ["help_command"], "Show help for a command"),
    ]:
        sp = sub.add_parser(name, help=desc)
        for o in opts:
            if o == "--target":
                sp.add_argument("--target", default="all", choices=list(PUBLISH_MAP) + ["all"])
            elif o == "--skip-build":
                sp.add_argument("--skip-build", action="store_true")
            elif o == "--uninstall":
                sp.add_argument("--uninstall", action="store_true")
            elif o == "--force":
                sp.add_argument("--force", action="store_true")
            elif o == "--fix":
                sp.add_argument("--fix", action="store_true")
            elif o == "--format":
                sp.add_argument("--format", action="store_true")
            elif o == "--no-bin":
                sp.add_argument("--no-bin", action="store_true")
            elif o == "--no-rules":
                sp.add_argument("--no-rules", action="store_true")
            elif o == "--verbose":
                sp.add_argument("--verbose", action="store_true")
            elif o == "--target-os":
                sp.add_argument(
                    "--target-os",
                    default="auto",
                    choices=["auto", "windows", "linux", "macos", "all"],
                    help="Target OS for executable (auto=current OS)",
                )
            elif o == "pytest_args":
                sp.add_argument("pytest_args", nargs=argparse.REMAINDER)
            elif o == "ruff_args":
                sp.add_argument("ruff_args", nargs=argparse.REMAINDER)
            elif o == "--target-version":
                sp.add_argument(
                    "--target-version", default=None, help="Target specific version in CHANGELOG (defaults to current)"
                )
            elif o == "paths":
                sp.add_argument("paths", nargs=argparse.REMAINDER)
            elif o == "help_command":
                sp.add_argument("help_command", nargs="?")

    return p


def main() -> None:
    # Intercept help flags before argparse to route to custom cmd_help
    if "-h" in sys.argv or "--help" in sys.argv:
        # Check if a specific command was requested (e.g. `run.py build -h`)
        command = None
        for arg in sys.argv[1:]:
            if arg not in ("-h", "--help", "--version") and not arg.startswith("-"):
                command = arg
                break
        cmd_help(command)
        sys.exit(0)

    parser = build_parser()
    args = parser.parse_args()

    if args.version:
        print(f"awlab-ai-assistant Development CLI  v{_get_version()} ({_get_build_tag()})")
        return

    match args.command:
        case None:
            cmd_help()
        case "build":
            cmd_build(
                no_bin=args.no_bin,
                no_rules=args.no_rules,
                target_os=args.target_os,
                verbose=getattr(args, "verbose", False),
            )
        case "publish":
            cmd_publish(
                target=args.target,
                skip_build=args.skip_build,
                force=args.force,
                uninstall=args.uninstall,
                no_bin=args.no_bin,
            )
        case "test":
            cmd_test(args.pytest_args)
        case "lint":
            cmd_lint(fix=args.fix, apply_format=args.format, paths=args.paths)
        case "ruff":
            cmd_ruff(args.ruff_args)
        case "compile-rules":
            cmd_compile_rules()
        case "release":
            cmd_release(args.target_version)
        case "help":
            cmd_help(args.help_command)
        case _:
            print(f"Unknown: {args.command}")
            parser.print_help()
            sys.exit(1)


if __name__ == "__main__":
    main()
