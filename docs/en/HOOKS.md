# Hook Registration (optional automation)

> [🏠 README](../../README.md) · [📚 Docs](../../README.md#documentation) · **Hook Registration**

Hooks are an **optional** zero-LLM automation layer on top of the MCP server. They let the
host fire the built exe (`dist/bin/awlab-ai-assistant.exe`) on lifecycle events (tool use,
prompt, session, stop) so user-pattern observations are captured automatically — with no
agent involvement and no LLM cost.

**Short answer: installing the MCP without hooks is fully supported and works fine.** Hooks
only add automatic capture. Read the [pros/cons](#pros--cons-of-enabling-hooks) below and
decide per host.

---

## Is the hook required? (No)

| Mode | How patterns get captured | Baking still runs? |
|------|---------------------------|--------------------|
| **MCP only** (no hooks) | The agent relays signals via `mem_observe` (part of its normal `action_call` usage) | ✅ Yes — every `action_call` runs an inline bake tick, and the background scheduler re-bakes active workspaces |
| **MCP + hooks** | The host captures command-carrying tool events automatically (zero LLM), plus `mem_observe` | ✅ Yes — same store, same pipeline |

**Conclusion:** the MCP server is the core; hooks are an additive convenience. You can ship
MCP-only and add hooks later without any migration.

---

## Pros & Cons of enabling hooks

| | Description |
|---|---|
| ✅ **Pros** | **Zero-LLM capture** — commands the user runs (e.g. `pnpm install`) are recorded as observations without spending a token. **Always-on** — capture happens even when the agent forgets to `mem_observe`. **Turn-end baking** — the `Stop` event runs the bake automatically. **Context injection** — prompt events can inject stack-scoped baked patterns into the host's context (READ path). **Self-loop safe** — anti-loop design: prompt events only inject, tool events only capture. |
| ⚠️ **Cons** | **Per-host setup** — one-time registration per agent (see below). **Per-event subprocess** — each hook fire spawns the exe once (small PyInstaller startup cost on every tool call). **Selective capture** — only command-carrying tool events write observations; file-read tools and prompt events don't (by design). **Path dependency** — configs point at a fixed exe path; moving/renaming the exe silently no-ops until you re-register. **Project resolution** — hosts whose payload lacks project context need `--project <path>` (or `CLAUDE_PROJECT_DIR`). |

---

## Prerequisites

1. A built executable: `python scripts/run.py build` → `dist/bin/awlab-ai-assistant.exe`.
2. Ready-made registration configs (written by every build) in `dist/profiles/hooks/`:
   `claude.hooks.json`, `hermes.hooks.yaml`, `copilot.hooks.txt`, `cline.hooks.txt`.

> The hook command is the same single exe as the MCP server — no second install.

---

## What each event does

Events map to an internal `kind` that decides the behaviour (anti-loop):

| Kind | Host events (examples) | Behaviour |
|------|------------------------|-----------|
| `prompt` | `UserPromptSubmit`, `pre_llm_call` | **READ + RELAY** — injects stack-scoped baked patterns; no capture |
| `tool` | `PostToolUse`, `post_tool_call` | **CAPTURE** — appends an observation when the tool carries a `command` |
| `pre_tool` | `PreToolUse` | **deviation check** — allow/block against stored patterns (currently allow) |
| `stop` | `Stop` | **BAKE** — runs the pipeline (key → count → consistency → confidence) |
| `session` / `subagent` | `SessionStart`, `SubagentStop` | observer-only (no action yet) |

Capture is **selective**: a Bash tool with `{"command": "pnpm install"}` writes an
observation; a `Read` tool (no command) does not — reading a file isn't a pattern signal.

---

## Register per agent

### 1) Claude Code

Merge the `hooks` block from `dist/profiles/hooks/claude.hooks.json` into
`~/.claude/settings.json` (create it if missing). Replace `awlab-ai-assistant.exe`
with the absolute path to your built exe:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [{ "type": "command", "command": "D:\\path\\to\\awlab-ai-assistant.exe hook --agent claude --event UserPromptSubmit" }] }
    ],
    "PostToolUse": [
      { "hooks": [{ "type": "command", "command": "D:\\path\\to\\awlab-ai-assistant.exe hook --agent claude --event PostToolUse" }] }
    ],
    "PreToolUse": [
      { "hooks": [{ "type": "command", "command": "D:\\path\\to\\awlab-ai-assistant.exe hook --agent claude --event PreToolUse" }] }
    ],
    "SubagentStop": [
      { "hooks": [{ "type": "command", "command": "D:\\path\\to\\awlab-ai-assistant.exe hook --agent claude --event SubagentStop" }] }
    ],
    "Stop": [
      { "hooks": [{ "type": "command", "command": "D:\\path\\to\\awlab-ai-assistant.exe hook --agent claude --event Stop" }] }
    ],
    "SessionStart": [
      { "hooks": [{ "type": "command", "command": "D:\\path\\to\\awlab-ai-assistant.exe hook --agent claude --event SessionStart" }] }
    ]
  }
}
```

Claude Code resolves the project from the payload (`cwd`) or `$CLAUDE_PROJECT_DIR`.

### 2) Hermes

Merge the `hooks:` block from `dist/profiles/hooks/hermes.hooks.yaml` into the Hermes
config (points at the same exe):

```yaml
hooks:
  pre_llm_call:
    - command: "D:\\path\\to\\awlab-ai-assistant.exe hook --agent hermes --event pre_llm_call"
  post_tool_call:
    - command: "D:\\path\\to\\awlab-ai-assistant.exe hook --agent hermes --event post_tool_call"
  pre_tool_call:
    - command: "D:\\path\\to\\awlab-ai-assistant.exe hook --agent hermes --event pre_tool_call"
  subagent_stop:
    - command: "D:\\path\\to\\awlab-ai-assistant.exe hook --agent hermes --event subagent_stop"
  on_session_start:
    - command: "D:\\path\\to\\awlab-ai-assistant.exe hook --agent hermes --event on_session_start"
  on_session_end:
    - command: "D:\\path\\to\\awlab-ai-assistant.exe hook --agent hermes --event on_session_end"
```

### 3) Cline

Cline hooks are registered in its settings UI (MCP/hook settings). Add the commands from
`dist/profiles/hooks/cline.hooks.txt`:

```
awlab-ai-assistant.exe hook --agent cline --event NewTask
awlab-ai-assistant.exe hook --agent cline --event PostToolUse
awlab-ai-assistant.exe hook --agent cline --event Stop
```

### 4) VS Code Copilot

Copilot doesn't read a hook-config file — registration goes through the host's
settings/UI (the mechanism is newer and version-dependent). Use the commands from
`dist/profiles/hooks/copilot.hooks.txt`:

```
awlab-ai-assistant.exe hook --agent copilot --event user-prompt-submit
awlab-ai-assistant.exe hook --agent copilot --event post-tool-use
awlab-ai-assistant.exe hook --agent copilot --event session-start
awlab-ai-assistant.exe hook --agent copilot --event session-end
awlab-ai-assistant.exe hook --agent copilot --event subagent-stop
awlab-ai-assistant.exe hook --agent copilot --event stop
```

---

## Verify a hook works

**Manually** (Linux/macOS use `printf`; on Windows use `cmd /c "echo ... | exe hook ..."` or
pipe from a script — note PowerShell `|` can be unreliable for native stdin):

```bash
# capture path (tool event with a command)
echo '{"tool_name":"Bash","tool_input":{"command":"pnpm install"}}' | \
  awlab-ai-assistant.exe hook --agent claude --event PostToolUse --project /path/to/project
# → writes /path/to/project/.ai/memory-bank/observations.jsonl
# → stdout: {}

# prompt path (READ)
echo '{"prompt":"please run the tests"}' | \
  awlab-ai-assistant.exe hook --agent claude --event UserPromptSubmit --project /path/to/project
# → stdout: {"decision":"allow"}
```

**Automated**: `python scripts/live_probe.py` (from the repo) includes a hook-capture check
— exit 0 / `35 passed` means the hook writes observations end-to-end.

---

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| Hook runs (exit 0) but no observation | Event was a prompt/read/no-command tool (by design). Use a command-carrying tool event, or the `Stop` event to bake. |
| No observation and no `.ai/project-id` created | The payload never reached the process — check stdin piping (PowerShell `\|` is unreliable; use `subprocess`/`cmd` redirection) and the exe path. |
| Project not resolved | Pass `--project <path>`, or ensure the payload has `cwd` / `CLAUDE_PROJECT_DIR`. |
| Hook silently does nothing | Exe path changed since registration — point the config at the current `dist/bin/awlab-ai-assistant.exe`. |
| Duplicate observations not growing | The dedup/delta guard is working — identical signals aren't double-counted. |
