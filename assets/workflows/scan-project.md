# Scan Project

description: Quick framework-aware scan of the current workspace before starting work.

## Step 1: Detect stack
- `log` Identify the framework and language from manifests (package.json, pyproject.toml, etc.)
- `agent_task` Confirm the detected stack with the user

## Step 2: Map structure
- `file_op` Read path: ".ai/project-id"
- `file_op` Read path: "AGENTS.md" if present

## Step 3: Report
- `log` Summarize stack, entry points, and test command in one short message
