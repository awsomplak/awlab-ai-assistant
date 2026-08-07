# Installation & CLI Reference

## Requirements

- **Python 3.10+** (for MCP server)
- **agent-recall** (knowledge graph backend)
- **AI model** with tool-calling support
- One of: **Cline**, **VS Code Copilot**, **Claude Code**, or **Hermes Agent**

Model pairing: 🟢 Simple → Local 1.5B–3B · 🟡 Medium → Local 14B–32B · 🔴 Complex → Frontier (Claude, GPT)

---

## Install MCP Server

```bash
# Clone
git clone https://github.com/awsomplak/cline-ai-assisted-dev.git
cd cline-ai-assisted-dev

# Recommended: create a virtual environment
python -m venv .venv
source .venv/bin/activate          # Linux/macOS
.venv\Scripts\activate              # Windows

# Install package (editable mode)
pip install -e .

# Install dev/test dependencies (optional)
pip install -e ".[dev]"
```

## Build Standalone Executables

```bash
# Build for current OS (uses PyInstaller)
python scripts/run.py build

# Build for specific targets
python scripts/run.py build --target-os=linux
python scripts/run.py build --target-os=all     # Specs for non-host OSes
```

Built binary at `dist/bin/`:

| Binary | Server | Exposed Tools |
|--------|--------|---------------|
| `awlab-mcp.exe` | `awlab-mcp` | `action_call` (dispatcher), `action_help` |

One consolidated executable — the `action_call` dispatcher routes to all operations
(plan, task, memory, graph, context, util, workflow).

Binaries are fully standalone — no Python or source files needed.

---

## Publish to AI Assistants

```bash
# First compile rules & skills into per-agent profiles
python scripts/run.py compile-rules

# Then publish to specific assistant(s)
python scripts/run.py publish --target=cline     # Cline
python scripts/run.py publish --target=copilot   # VS Code Copilot
python scripts/run.py publish --target=claude    # Claude Code
python scripts/run.py publish --target=hermes    # Hermes Agent
python scripts/run.py publish --target=all       # All assistants

# Uninstall
python scripts/run.py publish --uninstall
python scripts/run.py publish --uninstall --target=copilot
```

### Publish Targets

| Target | Rules | Skills |
|--------|-------|--------|
| `cline` | `~/Documents/Cline/Rules/` | `~/.agents/skills/` |
| `copilot` | `~/.copilot/instructions/` | `~/.agents/skills/` (shared) |
| `claude` | `~/.claude/CLAUDE.md` | `~/.claude/skills/` |
| `hermes` | — (packaged as skill) | `~/.hermes/skills/` |

---

## CLI Reference

```bash
python scripts/run.py <command> [options]
```

| Command | Description |
|---------|-------------|
| `compile-rules` | Compile rules + skills into per-agent profiles under `dist/profiles/` |
| `build` | Compile profiles + build Python package + standalone binaries → `dist/` |
| `publish` | Publish `dist/` contents to AI assistant locations |
| `test` | Run the pytest test suite |
| `help` | Show detailed help for a command |
| `--version` | Show version and build tag |

### compile-rules

```bash
python scripts/run.py compile-rules
```

Compiles `assets/rules/` (13 rule files) and `assets/skills/` (4 skills) into per-agent profiles:

```
dist/profiles/
├── cline/             # Individual .md files + skills
├── copilot/           # .instructions.md with YAML frontmatter + skills
├── claude/            # CLAUDE.md monolith + skills
├── hermes/            # Skills as SKILL.md subdirectories
└── .clinerules        # Project-level monolith (not published)
```

### build

```bash
# Full build (profiles + binaries)
python scripts/run.py build

# Skip binary build
python scripts/run.py build --no-bin

# Skip profile compilation
python scripts/run.py build --no-rules
```

### publish

```bash
# Publish all targets (builds first if /dist missing)
python scripts/run.py publish

# Publish to a single target
python scripts/run.py publish --target=claude

# Skip automatic build
python scripts/run.py publish --target=all --skip-build

# Force (skip confirmation prompts)
python scripts/run.py publish --force

# Remove installed files
python scripts/run.py publish --uninstall
```

---

## Quick Start (by Assistant)

### Cline

```bash
# 1. Install
pip install -e .
python scripts/run.py compile-rules

# 2. Publish
python scripts/run.py publish --target=cline

# 3. In VS Code with Cline extension:
#    - "follow rules"    → load registry & plan
#    - "create plan"     → create new implementation plan
#    - "start phase 1"   → execute first phase
```

### VS Code Copilot

```bash
# 1. Install & compile
pip install -e .
python scripts/run.py compile-rules

# 2. Publish
python scripts/run.py publish --target=copilot

# 3. In VS Code Copilot chat:
#    - `/create plan`    → create new plan
#    - `/plan-status`    → check current plan
#    - `/retrospective`  → generate plan retrospective
```

### Claude Code

```bash
# 1. Install & compile
pip install -e .
python scripts/run.py compile-rules

# 2. Publish
python scripts/run.py publish --target=claude
```

### Hermes Agent

```bash
# 1. Install & compile
pip install -e .
python scripts/run.py compile-rules

# 2. Publish
python scripts/run.py publish --target=hermes
```
