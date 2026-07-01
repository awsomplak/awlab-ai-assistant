#!/usr/bin/env python3
"""
AWLab-ID Development CLI — Single entry point for all project operations.

Usage:
    run.py build [--no-bin] [--no-rules]
    run.py publish [--target=<name>] [--skip-build] [--force]
    run.py test [<pytest-args>...]
    run.py compile-rules
    run.py help [<command>]
    run.py --version

Commands:
    build           Build Python package + compile rules/skills → /dist
    publish         Publish /dist to AI assistant locations
    test            Run the test suite
    compile-rules   Compile rules to assistant-specific profiles
    help            Show this message or help for a specific command
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# ══════════════════════════════════════════════════════════════════════════
#  Constants
# ══════════════════════════════════════════════════════════════════════════

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
RULES_SRC = ROOT / "assets" / "rules"
SKILLS_SRC = ROOT / "assets" / "skills"
PROFILES_DIR = ROOT / "assets" / "profiles"
PYTHON_SRC = ROOT / "src" / "mcp_server"
TEST_DIR = ROOT / "tests"

RULE_ORDER = [
    "00-meta.md", "05-environment.md", "02-plan-artifacts.md",
    "01-memory-bank.md", "03-token-strategies.md", "06-project-scanner.md",
    "07-model-router.md", "04-commands.md",
    "08-project-id.md", "09-user-patterns.md", "10-pattern-lifecycle.md",
    "11-agent-memory-isolation.md", "12-agent-mcp-workspace-path.md",
]

MCP_TOOLS = """
  ── awlab-mcp (Utility & Context) ──
    ctx_get_snapshot, ctx_read_memory_bank, ctx_scan_project,
    ctx_suggest_files, util_get_version, util_get_project_meta

  ── awlab-plan (Registry, Tasks & Workflows) ──
    reg_list_registry, reg_switch_active_plan, reg_validate_phase_gate,
    reg_get_next_eligible_task, reg_mark_phase_complete,
    reg_resolve_deferred_tasks, reg_check_plan_completable,
    reg_generate_retrospective,
    task_read_plan_tasks, task_update_status, task_batch_update,
    task_validate_transition, task_write_plan_tasks, task_format_markdown,
    wf_execute, wf_list,
    util_generate_mermaid

  ── awlab-memory (Memory & Context Store) ──
    mem_search (unified — accepts project_id + scope + use_dense),
    mem_create_entities, mem_tag_entity,
    mem_relate, mem_fetch_node_details, mem_read_graph,
    mem_archive_entities, mem_delete_observations, mem_delete_relations,
    mem_store, mem_list_patterns,
    ctx_store, ctx_get_fragment
"""


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

Build the entire project to /dist.  /dist is always cleaned before build.

Options:
  --no-bin            Skip Python package build
  --no-rules         Skip rules/skills compilation
  --target-os=OS     Target OS: auto (default), windows, linux, macos, all

Output:
  dist/
  ├── build-manifest.json
  ├── bin/               # Executable(s) + source fallback
  ├── rules/
  ├── profiles/
  └── skills/

Examples:
  run.py build                        # Build for current OS
  run.py build --target-os=all        # Build for all OSes (specs for non-host)
  run.py build --target-os=linux      # Build spec for Linux
""",
        "publish": """\
Usage: run.py publish [options]

Publish /dist contents to AI assistant locations.
Use --uninstall to remove previously installed files.

Options:
  --target=<name>   One of: cline, copilot, claude, hermes, all
  --skip-build      Fail if /dist doesn't exist instead of building
  --force           Skip confirmation prompts
  --uninstall       Remove installed files instead of installing

Target Paths:
  Skills:
    cline     ~/.agents/skills/
    copilot   ~/.agents/skills/ (shared with Cline)
    claude    ~/.claude/skills/
    hermes    ~/.hermes/skills/

  Rules:
    cline     ~/Documents/Cline/Rules/
    copilot   ~/.copilot/instructions/
    claude    ~/.claude/CLAUDE.md
    hermes    ~/.hermes/skills/
""",
        "test": """\
Usage: run.py test [<pytest-args>...]

Run the test suite. Passes all additional arguments to pytest.

Examples:
  run.py test                    # Run all tests
  run.py test -k "test_plan"     # Run tests matching pattern
  run.py test --tb=long          # Verbose traceback
""",
        "compile-rules": """\
Usage: run.py compile-rules

Compile rules from assets/rules/ into assistant-specific profiles
in assets/profiles/.

Output:
  assets/profiles/
  ├── claude/              (global skills and CLAUDE.md monolith)
  ├── cline/               (global skills and rules for cline)
  ├── copilot/             (global skills and rules for copilot)
  ├── hermes/              (global skills and rules for hermes)
  └── .clinerules          (Cline per project rules ready to copy)
""",
    }

    if command and command in texts:
        print(f"\n  {Style.BOLD}run.py {command}{Style.RESET}")
        print(f"  {'=' * (len(command) + 7)}\n{texts[command]}")
        return

    print(__doc__)
    print(f"\n  {Style.BOLD}Available MCP Tools:{Style.RESET}{MCP_TOOLS}")
    print(f"\n  Project: {ROOT}")
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
        head = full[:m.start(1) - 1] if m.start(1) > 0 else ""
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


def _compile_cline(rules: list[dict], skills: list[dict], profiles_dir: Path) -> None:
    """Cline: individual .md files + skills + .clinerules monolith."""
    cline_dir = profiles_dir / "cline"
    (cline_dir / "rules").mkdir(parents=True, exist_ok=True)
    for r in rules:
        # Individual files keep HTML comments, rewrite refs to heading anchors
        content = _rewrite_refs(r["content"])
        (cline_dir / "rules" / r["filename"]).write_text(content, "utf-8")
    _ok("cline/rules/  (13 individual files, HTML comments preserved, heading anchors)")

    # Skills for Cline
    _copy_skills(skills, cline_dir, "cline/skills/")

    # .clinerules monolith (project-level) \u2014 strip HTML comments + rewrite refs
    stripped = [{"filename": r["filename"], "content": _rewrite_refs(_strip_html_comments(r["content"]))} for r in rules]
    unified = _build_unified(stripped)
    (profiles_dir / ".clinerules").write_text(
        f"# Cline Rules \u2014 AWLab-ID\n\n{unified}\n\n## Available MCP Tools\n{MCP_TOOLS}\n", "utf-8"
    )
    _ok(".clinerules  (monolith, HTML comments stripped, heading anchors)")


def _compile_copilot(rules: list[dict], skills: list[dict], profiles_dir: Path) -> None:
    """Copilot: individual .instructions.md with YAML frontmatter, stripped comments, offset headings."""
    copilot_dir = profiles_dir / "copilot"
    copilot_dir.mkdir(parents=True, exist_ok=True)

    descriptions = {
        "00-meta": "Rule priority, conflict resolution, and deep analysis protocol",
        "01-memory-bank": "Knowledge-graph memory operations via awlab-memory MCP tools",
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
    }
    for r in rules:
        base = r["filename"].replace(".md", "")
        desc = descriptions.get(base, f"AWLab-ID rule: {base}")
        # Strip HTML comments, offset headings, rewrite refs to heading anchors
        cleaned = _offset_headings(_strip_html_comments(r["content"]), levels=1)
        cleaned = _rewrite_refs(cleaned)
        frontmatter = f"---\nname: {base}\ndescription: '{desc}'\n---\n\n"
        (copilot_dir / f"{base}.instructions.md").write_text(frontmatter + cleaned, "utf-8")
    _ok("copilot/  (13 .instructions.md files, comments stripped, headings offset, heading anchors)")


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
        f"# Claude Code — AWLab-ID\n\n"
        f"MCP Tools via agent-memory:\n{MCP_TOOLS}\n\n## Rules\n\n{unified}\n", "utf-8"
    )
    _ok("claude/CLAUDE.md  (monolith, comments stripped, heading anchors)")

    # Skills for Claude Code
    _copy_skills(skills, claude_dir, "claude/skills/")


def _compile_hermes(rules: list[dict], skills: list[dict], profiles_dir: Path) -> None:
    """Hermes: rules as SKILL.md + all skills in ~/.hermes/skills/."""
    hermes_dir = profiles_dir / "hermes" / "skills"
    hermes_dir.mkdir(parents=True, exist_ok=True)

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
        f"description: AWLab-ID rules for plan management, cross-session memory, "
        f"project scanning, and AI-assisted development conventions\n"
        f"applyTo: '**/*'\n"
        f"---\n"
        f"\n"
        f"# AWLab-ID Rules\n\n"
        f"MCP Tools:\n{MCP_TOOLS}\n\n{unified}\n", "utf-8"
    )
    _ok("hermes/skills/awlab-rules/SKILL.md  (skill-packaged rules, comments stripped, anchors)")

    # ── Copy all existing skills ──
    for s in skills:
        d = hermes_dir / s["name"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(s["content"], "utf-8")
    _ok(f"hermes/skills/  ({len(skills)} skills copied)")


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

    _detail(f"{len(rules)} rules, {len(skills)} skills")
    return rules, skills


# ══════════════════════════════════════════════════════════════════════════
#  Build
# ══════════════════════════════════════════════════════════════════════════

def _detect_current_os() -> str:
    """Detect the current operating system."""
    import platform
    sys_platform = platform.system().lower()
    if sys_platform == "windows":
        return "windows"
    if sys_platform == "darwin":
        return "macos"
    return "linux"


def _exe_name(base: str, target_os: str) -> str:
    """Return the executable filename for the given OS."""
    return f"{base}.exe" if target_os == "windows" else base


def cmd_build(no_bin: bool = False, no_rules: bool = False, target_os: str = "auto") -> None:
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

    # Always clean /dist before build to prevent stale/mixed artifacts
    try:
        if DIST.exists():
            shutil.rmtree(DIST)
            _ok("Cleaned /dist")
        DIST.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        _warn("Permission denied while cleaning /dist")
        _warn("The awlab-mcp binary may still be running — please stop it first, then retry.")
        sys.exit(1)

    # 1. Bump build tag
    vf = PYTHON_SRC / "_version.py"
    raw = vf.read_text("utf-8")
    m = re.search(r'__build_tag__\s*=\s*"build\.(\d+)"', raw)
    if m:
        new = f"build.{int(m.group(1)) + 1:03d}"
        raw = raw.replace(m.group(0), f'__build_tag__ = "{new}"')
        vf.write_text(raw, "utf-8")
        _info(f"Tag: {m.group(0).split('=')[1].strip().strip('\"')} → {new}")

    # 2. Compile rules → dist/profiles/{agent}/
    if not no_rules:
        _info("Compiling assets...")
        rules, skills = cmd_compile_rules()

        # Copy per-agent profiles to dist
        if PROFILES_DIR.exists():
            shutil.copytree(PROFILES_DIR, DIST / "profiles", dirs_exist_ok=True)
            _detail("profiles/  (per-agent: cline, copilot, claude, hermes)")

        _ok(f"{len(rules)} rules, {len(skills)} skills")

    # 3. Python package
    if not no_bin:
        _info("Building Python package...")
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-e", str(ROOT)],
            capture_output=True, text=True, cwd=str(ROOT),
        )
        if r.returncode:
            _fail(f"pip install failed:\n{r.stderr}")

        py_dist = DIST / "bin"
        py_dist.mkdir(parents=True, exist_ok=True)

        pe = shutil.which("pyinstaller") or shutil.which("pyinstaller.exe") or (ROOT / ".venv" / "Scripts" / "pyinstaller.exe")
        has_pyinstaller = pe and Path(pe).exists()

        for target in build_targets:
            if target != current_os:
                _warn(f"Cannot build for {target} from {current_os}. Build on a {target} machine instead.")
                # Generate a .spec file so the user can build on the target OS
                spec_path = ROOT / f"awlab-mcp-{target}.spec"
                spec_path.write_text(
                    f"# PyInstaller spec for awlab-mcp (target: {target})\n"
                    f"# Copy this file to a {target} machine and run:\n"
                    f"#   pyinstaller awlab-mcp-{target}.spec\n"
                    f"#\n"
                    f"# Auto-generated by run.py build --target-os={target}\n"
                    f"# (Replace paths if the project root differs)\n",
                    "utf-8"
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

            # ── Build 3 separate MCP server binaries ─────────────────────
            BINARIES = [
                ("awlab-mcp", "__main__.py", [
                    "mcp_server", "mcp_server.config",
                    "mcp_server.modules.lifecycle", "mcp_server.modules.registration",
                    "mcp_server.helpers", "mcp_server.tools",
                    "mcp_server.tools.context_tools", "mcp_server.tools.file_tools",
                    "mcp_server.tools.utils_tools",
                ]),
                ("awlab-plan", "__main_plan__.py", [
                    "mcp_server", "mcp_server.config",
                    "mcp_server.modules.lifecycle", "mcp_server.modules.registration_plan",
                    "mcp_server.helpers", "mcp_server.tools",
                    "mcp_server.tools.plan_tools", "mcp_server.tools.utils_tools",
                ]),
                ("awlab-memory", "__main_memory__.py", [
                    "mcp_server", "mcp_server.config",
                    "mcp_server.modules.lifecycle", "mcp_server.modules.registration_memory",
                    "mcp_server.helpers",
                    "mcp_server.tools.context_tools",
                    "mcp_server.helpers.hybrid_search", "mcp_server.helpers.embeddings",
                ]),
            ]

            pkg_src = str(PYTHON_SRC)
            pkg_dest = "mcp_server"
            sep = ";" if target == "windows" else ":"

            for bin_name, entry_module, hidden_imports in BINARIES:
                _info(f"Building {bin_name} for {target}...")
                exe_name = _exe_name(bin_name, target)
                cmd = [
                    str(pe), "--onefile", "--distpath", str(py_dist),
                    "--name", bin_name,
                    "--paths", str(PYTHON_SRC.parent),
                    "--add-data", f"{pkg_src}{sep}{pkg_dest}",
                ]
                for hi in hidden_imports:
                    cmd.extend(["--hidden-import", hi])
                cmd.append(str(PYTHON_SRC / entry_module))

                result = subprocess.run(
                    cmd, capture_output=True, text=True, cwd=str(ROOT),
                )

                # Clean PyInstaller per-binary artifacts
                spec_name = f"{bin_name}.spec"
                for p in [ROOT / "build", ROOT / spec_name]:
                    if p.is_dir():
                        shutil.rmtree(p)
                    elif p.exists():
                        p.unlink()

                if result.returncode == 0:
                    _ok(f"Executable: {py_dist / exe_name}")
                else:
                    _warn(f"PyInstaller ({bin_name}/{target}): {result.stderr[-300:]}")

    # Clean up generated .spec files after build
    for spec_file in ROOT.glob("awlab-mcp-*.spec"):
        try:
            spec_file.unlink()
        except OSError:
            pass

    # 4. Manifest
    (DIST / "build-manifest.json").write_text(json.dumps({
        "version": _get_version(), "buildTag": _get_build_tag(),
        "buildTime": datetime.now(timezone.utc).isoformat(),
    }, indent=2), "utf-8")

    print(f"\n  {Style.GREEN}{Style.BOLD}\u2713 Build complete{Style.RESET}  {Style.DIM}\u2192 {DIST}{Style.RESET}")


# ══════════════════════════════════════════════════════════════════════════
#  Publish
# ══════════════════════════════════════════════════════════════════════════

PUBLISH_MAP = {
    "cline": ("Cline", [
        ("profiles/cline/rules", "{home}/Documents/Cline/Rules/"),
        ("profiles/cline/skills", "{home}/.agents/skills"),
    ]),
    "copilot": ("Copilot", [
        ("profiles/copilot", "{home}/.copilot/instructions"),
        ("profiles/cline/skills", "{home}/.agents/skills"),
    ]),
    "claude": ("Claude", [
        ("profiles/claude/CLAUDE.md", "{home}/.claude/CLAUDE.md"),
        ("profiles/claude/skills", "{home}/.claude/skills"),
    ]),
    "hermes": ("Hermes", [
        ("profiles/hermes/skills", "{home}/.hermes/skills"),
    ]),
}


def _resolve_dest(dest_tpl: str, home: Path, name: str = "") -> Path:
    return Path(dest_tpl.replace("{home}", str(home)).replace("{name}", name))


def cmd_publish(target: str = "all", skip_build: bool = False, force: bool = False, uninstall: bool = False) -> None:
    if uninstall:
        _header("Uninstalling")
        home = Path.home()
        targets = [target] if target != "all" else list(PUBLISH_MAP)
        total = 0
        for t in targets:
            if t not in PUBLISH_MAP:
                continue
            label, mappings = PUBLISH_MAP[t]
            _info(f"Removing: {label}")
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

    if not DIST.exists():
        if skip_build:
            _fail("/dist not found. Run build first.")
        _info("/dist not found — building first\n")
        cmd_build()

    mf = DIST / "build-manifest.json"
    if mf.exists():
        m = json.loads(mf.read_text("utf-8"))
        _info(f"v{m['version']} ({m['buildTag']}) — built {m['buildTime'][:19]}")

    home = Path.home()
    targets = [target] if target != "all" else list(PUBLISH_MAP)
    total = 0

    for t in targets:
        if t not in PUBLISH_MAP:
            _warn(f"Unknown target: {t}")
            continue
        label, mappings = PUBLISH_MAP[t]
        _header(f"Publishing: {label}")

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
                shutil.copy2(src, dest)
                _detail(f"{dest}")
                total += 1
            else:
                _warn(f"{src_rel} not found")

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


# ══════════════════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════════════════

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run.py",
        description="AWLab-ID Development CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="store_true", help="Show version")

    sub = p.add_subparsers(dest="command")

    for name, opts, desc in [
        ("build", ["--no-bin", "--no-rules", "--target-os"], "Build everything to /dist"),
        ("publish", ["--target", "--skip-build", "--force", "--uninstall"], "Publish /dist to AI assistants"),
        ("test", ["pytest_args"], "Run test suite"),
        ("compile-rules", [], "Compile rules to assistant profiles"),
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
            elif o == "--no-bin":
                sp.add_argument("--no-bin", action="store_true")
            elif o == "--no-rules":
                sp.add_argument("--no-rules", action="store_true")
            elif o == "--target-os":
                sp.add_argument("--target-os", default="auto",
                    choices=["auto", "windows", "linux", "macos", "all"],
                    help="Target OS for executable (auto=current OS)")
            elif o == "pytest_args":
                sp.add_argument("pytest_args", nargs=argparse.REMAINDER)
            elif o == "help_command":
                sp.add_argument("help_command", nargs="?")

    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.version:
        print(f"AWLab-ID Development CLI  v{_get_version()} ({_get_build_tag()})")
        return

    match args.command:
        case None:
            parser.print_help()
        case "build":
            cmd_build(no_bin=args.no_bin, no_rules=args.no_rules, target_os=args.target_os)
        case "publish":
            cmd_publish(target=args.target, skip_build=args.skip_build, force=args.force, uninstall=args.uninstall)
        case "test":
            cmd_test(args.pytest_args)
        case "compile-rules":
            cmd_compile_rules()
        case "help":
            cmd_help(args.help_command)
        case _:
            print(f"Unknown: {args.command}")
            parser.print_help()
            sys.exit(1)


if __name__ == "__main__":
    main()
