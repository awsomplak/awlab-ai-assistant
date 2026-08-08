# Workflows (shared source)

This directory is the **single source of truth** for AWLab-ID workflows. `run.py build`
copies these `.md` files into `dist/workflows/`, and `run.py publish` distributes them:

| Agent | Destination |
| --- | --- |
| **Cline** | `~/Documents/Cline/Workflows/` **and** `~/.awlab-id/agent-memory/work-flows/` |
| **Copilot / Claude / Hermes** (non-Cline) | `~/.awlab-id/agent-memory/work-flows/` |

## Runtime

The MCP server (`wf` action) loads workflows from the shared location by default:
`~/.awlab-id/agent-memory/work-flows/`. `workspace_path` is optional — workflows are
workspace-independent step definitions. Pass `workflows_dir` to override the location.

## Format

Workflow files are Markdown with YAML-ish frontmatter and `##` step sections:

```markdown
# Workflow Name

description: One-line summary

## Step 1: Setup
- `log` Describe what happens here
- `agent_task` Ask the user a question

## Step 2: Execute
- `mcp_tool` Call tool_name: my_tool
- `file_op` Read path: "src/app.py"
```

Step action types: `log`, `mcp_tool`, `file_op`, `agent_task`.
