<!-- → authority: 00-meta.md -->
# Project ID Auto-Detection

## Purpose
Generate a stable identifier from the workspace folder name. This ID is used by the wrapper script to set `AGENT_RECALL_SLUG`, which awlab-memory uses for project isolation.

## Bootstrap (Run on every `follow rules`)

1. Get the project root.
2. Extract the last segment of the path → lowercase, replace non‑alphanumeric with `_`.
3. Store it in `.ai/project-id` (plain text).

## Usage
- The wrapper script reads this file and sets `AGENT_RECALL_SLUG`.
- awlab-memory isolates all memories under that slug automatically.
