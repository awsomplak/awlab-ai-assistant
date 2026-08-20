# Changelog
## [3.0.4]

### Added
- **Large-graph HTML viz (graph.html / family.html)** — every rendered visualization now embeds a self-contained client-side layer so projects with thousands of nodes stay usable:
  - **Filter bar** — filter by file path (`src/components`), by minimum degree (de-hairball), or **Focus 2-hop** (neighborhood of the selected node); edges clip to the visible nodes; **Reset** restores the initial view.
  - **Physics guard** — above ~2000 visible nodes the forceAtlas2 layout is disabled (would freeze the browser); narrow the view, then **Stabilize** re-runs the layout.
  - **Community drill-down** — when a graph is over `node_limit` (aggregated community view), the full member node/edge dataset is embedded alongside the meta-graph; clicking a community node opens a searchable member list and **Load members into graph** rebuilds the view from that community's member subgraph.
  - **Heuristic community labels (non-LLM)** — top-degree member labels per community (e.g. `apiHandler, helper`) now populate the Communities legend (previously only "Select All") and the drill-down/node-info headings; `to_html` receives `community_labels` for both full and aggregated views.
  - **Resizable sidebar panes** — a draggable splitter between **Node Info** and **Communities** lets either pane take more room (double-click resets); the Node Info panel is `flex: 0 0 auto` + capped + internally scrollable so a long neighbor list never overlaps or pushes the Communities legend.
  - **Collapsible Filter bar** — a **Filters** header button collapses/expands the filter controls to free sidebar space.
- **Non-blocking `graph_build`** — `background` now defaults to `true`: the call is a fire-and-forget trigger (returns `triggered: true, background_started: true`) and the chunk worker drains `remaining_files` to 0; graph reads return `mode: "pending"` while a build is incomplete so stale/partial data is never served.

### Fixed
- **Background chunk-worker stall on large projects (eka-panel)** — a bare `*` in a Laravel-style nested `.gitignore` (`*` + `!.gitignore` re-inclusions) became a GLOBAL basename glob that excluded every file, so `_source_manifest` returned 0, the incremental/chunked branch was skipped, and a full-corpus extract ran in ONE call ignoring `chunk_size`. `.gitignore` + `.graphignore` now parse into ONE additive rule set with identical file/dir glob semantics; bare `*`/`**` ignore-all lines are skipped; `_source_manifest` has a blank-detection guard. Verified end-to-end on eka-panel: the background worker drains `remaining_files` to 0 (2673 → 0) and reaches `fresh: true`.
- **`background: false` blocked by a stale in-flight guard (Bug 3)** — a synchronous build now always proceeds (serialized on a bounded per-project lock) instead of returning "rebuild already in progress"; `force: true` bypasses the guard entirely.
- **`graph_status` staleness** — `processed_files` is now cumulative (`total − remaining`); a partial build reports `exists: true`; `rebuilding` reflects a live worker and a stale persisted flag is auto-cleared on read; `background_error` surfaces worker failures and survives server restarts.
- **Worker lifecycle hardening** — the chunk-drain loop distinguishes `remaining_files` `None` vs `0`; a stall watchdog surfaces a real `background_error` and stops treating a zombie thread as in-flight; the per-project build lock is bounded (600s).

### Changed
- **Exclusion visibility** — `graph_status` reports `scanned_files` / `excluded_files` / `supported_files` (scanned count memoized); `.gitignore` / `.graphignore` are no longer counted as graph source.

### Builds
- `v3.0.4+build.103` — non-blocking fire-and-forget background graph builds (pending-read guards).
- `v3.0.4+build.104` — large-graph HTML viz (filter bar + physics guard + community drill-down + heuristic community labels + resizable panes + collapsible Filter bar) + graph background-worker stall fix (unified exclusion engine, worker hardening, persisted rebuilding lifecycle, graph_status accuracy, force). Tests + lint clean.

## [3.0.3] - 2026-08-19

### Added
- **Chunked graph builds (queue-chunk semantics)** — `graph_build` now processes the corpus in bounded chunks so peak RAM/CPU stays flat on large projects:
  - `chunk_size` / `GRAPH_CHUNK_SIZE` (default `200`) — max files processed per build; each run advances the freshness manifest by exactly that many.
  - `max_files` / `GRAPH_MAX_FILES` — optional cap on the FIRST build's leading corpus (partial first build; the rest is folded in incrementally).
  - `background` — after the synchronous chunk, a Laravel-queue-style background worker keeps advancing chunks until the graph is complete (`remaining_files == 0`); graph reads auto-start it via `graph_fresh` → `ensure_fresh(background=True)`.
  - Progress reporting — `graph_build` / `graph_status` return `processed_files`, `remaining_files`, `chunked`; `graph_status` stays `fresh: false` until the graph is complete.
- **Graceful HTML viz limit** — `node_limit` param + `graph_viz_limit` config (default `20000`, overridable via `GRAPHIFY_VIZ_NODE_LIMIT`): graphs over the limit render the aggregated community meta-graph view instead of raising `ValueError`; `0` disables the HTML export; a failed HTML export is non-fatal (graph.json + manifest always land).
- **`.graphignore` exclusion file** — gitignore syntax, combined ADDITIVELY with the project's `.gitignore` (parsed at every directory level): exclude files/directories from the code graph only (generated code, vendored copies, …) without ever affecting git. A `.graphignore` change triggers a rebuild.
- **Documentation** — `.graphignore` + chunked-build behavior documented in the generated per-project `.ai/codegraph/README.md` and in `docs/en/` + `docs/id/` (AVAILABLE_TOOLS graph sections + INSTALL env rows `GRAPH_CHUNK_SIZE` / `GRAPH_MAX_FILES`).

### Changed
- **Test suite compaction** — merged repetitive clusters into loop/combined tests (`test_workspace` 22→6, `test_context_tools` 27→13, graph scalability 11→7) and parametrized the status-transition / validation clusters in `test_plan_tools` + `test_integration_mcp_tools` — same coverage, 368 tests.
- **`.gitignore` support unchanged** — `.graphignore` is parsed alongside it, never replacing it.

### Fixed
- **Oversized-graph build failure** — a graph over the HTML viz limit previously raised `ValueError` *after* writing graph.json but *before* `.build_state.json` (→ stale-manifest rebuild loop); it now renders an aggregated community view (or skips HTML) and the build completes normally.

### Builds
- `v3.0.3+build.102` — chunked graph builds, graceful HTML viz limit, `.graphignore` exclusion, docs en/id + test compaction. 368 tests pass, lint + format clean.

## [3.0.2] - 2026-08-16

### Added
- **Pattern-baking core (Phase 4)**: append-only observation store (`.ai/memory-bank/observations.jsonl`, torn-tail tolerant) + deterministic LLM-free bake engine (`key → count → consistency → confidence`) persisting candidates to `baked.json`; async background bake scheduler + per-action inline bake tick.
- **`mem_observe` action** — records user-pattern evidence (signals) into the observation store, dedup/delta-guarded by fingerprint; feeds the baking pipeline.
- **`project_id` check-and-create action** — idempotent; auto-creates `.ai/project-id` from the sanitized directory-name slug so memory isolation never falls through to the global DB. STRICT FIRST-CALL rule (rules 01/08).
- **`plan_doc` action** — direct read/write/delete of a plan's `plan.md`/`notes.md` (full content, no template / IDE compare).
- **Unified hook mode** — `awlab-ai-assistant.exe hook --agent <host> --event <event>` with per-host adapters (Hermes/Claude/Copilot/Cline), anti-loop dispatch, and project resolution; compiled per-host hook configs (Claude JSON / Hermes YAML).
- **Baked-pattern delivery** — `ctx_info mode="context"` and `mem_search` inject stack-scoped `pattern_candidates` / `baked_patterns` with a tell-once delivery marker; `## Patterns` section in `context.md`.
- **Shared `awlab-baker` subagent** (`assets/agents/awlab-baker.md`) — observe → mine → bake → report protocol (Claude format, also read by Copilot).
- **OpenCode profile** — global `AGENTS.md` + `skills/<name>/SKILL.md` + `opencode.mcp.json` wiring in compile + publish.
- **Live probe script** (`scripts/live_probe.py`) — smoke-tests the built exe over real stdio MCP (action surface, baking, plan/memory/graph lifecycle, hook mode).
- **Indonesian documentation** — `docs/id/` (AVAILABLE_TOOLS, HOOKS, INSTALL) + `README_ID.md`; English docs moved to `docs/en/`; README redesigned with banner + language switcher.

### Changed
- **Action surface 20 → 23** — `project_id`, `plan_doc`, `mem_observe` join the 20-action `REGISTRY`.
- **Docs reorganized** — `docs/` split into `docs/en/` + `docs/id/`; `REGISTRY_SCHEMA.md` moved to `docs/en/`.
- **Rules updated for pattern baking** — `01` (STRICT FIRST-CALL), `02` (notes.md discipline), `08` (project-id check-and-create), `09` (observation-driven capture + baking tiers), `10` (computed confidence + stack tagging + delivery), `11` (23 actions).

### Fixed
- **Markdown table cells with literal `|`** now round-trip via `\|` escaping in registry parsing (legacy rows with a raw `|` still parse, surplus cells rejoined into the summary).
- **Task descriptions with embedded newlines** are normalized to a single line so they can't produce malformed `tasks.md` entries.

### Builds
- `v3.0.2+build.100` — pattern-baking core (Phase 4), 23-action surface, hook mode, docs en/id split. 391 tests pass, lint + format clean.

## [3.0.1] - 2026-08-08

### Added
- **Code knowledge graph (graph)**: per-project structural graph via `graphifyy` (AST-only, no LLM) — `graph_build`, `graph_status`, `graph_query`, `graph_path`, `graph_explain` under a single `graph` action group. The graph html will genereted at `{project_root}/.ai/codegraph/graph.html`.
- **Incremental graph rebuild**: `graph_build` re-extracts only changed source files (unchanged corpus passed as resolution context) and merges into the prior graph — ~40x faster than a full rebuild, with output identical to a full rebuild at the same source state. Auto-refresh via the `graph_fresh` precondition is now cheap.
- **Scratch/temp file hygiene rule** (`13-file-hygiene.md`): strict temp-file placement in `.ai/temp/` (gitignored) — no scratch files in the project root.
- **Background non-blocking graph rebuild**: heavy stale/first builds run in a background thread so reads never block; small incremental rebuilds stay synchronous for accuracy.
- **Graph freshness contract**: every graph read (`graph_query`/`graph_path`/`graph_explain`) returns `graph_fresh`, `graph_exists`, `graph_rebuilding`, `graph_built_at`; explicit `graph_build` coalesces with an in-flight background rebuild.
- **Environment-variable documentation** (`docs/en/INSTALL.md`): `AWLAB_ENV`, `LOG_ENABLED`, `LOG_LEVEL`, `DB_PATH`, `GRAPH_PARALLEL` with resolution order (env → config.json → default).
- **Project families (schema v2)** — `project-families.json` members are now `[{path, project_id}]` objects (no role key; `project_id` is the stable member identity, so multiple same-role members like 2 frontends/plugins work). Family keys support `-` (`eka-warehouse`); the legacy `{slug: [paths]}` shape is still accepted.
- **Project-id resolution & reconciliation** — a member's own `.ai/project-id` file is authoritative over the declared `project_id` (then `<slug>-<dir>` derived). `sync_family_project_ids()` reconciles the family JSON to each project's file when they differ (e.g. `eka-warehouse` vs `eka_warehouse`) and runs a duplicate checker (same `project_id` on different paths → the later member derives a distinct id + the conflict is reported). Fresh members are seeded with a tiny `.ai/project-id` marker on family build. Family graph `repo::` tags = resolved member `project_id`.
- **Offline cache / `mem_replay`** — a new `mem_replay` action (19th) drains the offline cache at `.ai/memory-bank/pending.jsonl` (JSONL — one JSON object per line). Mutations are queued there instead of dropped when a store write fails (`mem_write`/`mem_remove` store down, `task_update` DB-sync down) or when the MCP server is unreachable. Successful entries are removed, failed ones kept for retry; `dry_run` previews.
- **`14-mcp-offline-cache` rule** — agent-side protocol: when the MCP server is down, queue intended `mem_write`/`mem_remove`/`task_update` to `pending.jsonl` with your own file tools, never claim success, and replay via `mem_replay` on recovery. Compiled into all agent profiles (14 rules).
- **`reg_update` — single registry.md CRUD** — `create` (server-generated UUID, no agent UUID thinking; Active ⏹️) / `update` (status `active|paused|complete` → move the row to the correct table, refresh `Date`, keep the immutable `Created At` column, optional summary) / `delete` (strict user approval via `confirmed=true`; the server refuses without it). registry.md now has an immutable `Created At` column (legacy 4-column rows still parse). Replaces the one-off plan-complete path — one registry.md action.
- **Vite/JS path-alias import indexing** — a post-build pass reads `resolve.alias` from `vite.config.*` / `nuxt.config.*` (object or array form, string or `fileURLToPath(new URL(...))` replacements) and emits the missing `imports_from`/`imports` edges for `@/...`, `@pages/...`, `~/...` imports that graphifyy cannot resolve (it only reads tsconfig/jsconfig `paths`). `.vue` SFCs and any alias-importing file now stay connected in `graph_path` even with no `tsconfig.json`. Handles multi-segment keys via longest-prefix match, is idempotent, and self-heals previously-built graphs on their next no-op read.

### Changed
- **MCP tool consolidation**: 36 tools across 3 servers → single mcp server with **2 tools** (`action_call` + `action_help`) routing **20 actions** (incl. `graph_*`, the `mem_list_entities` + `mem_dedupe` memory-auditing actions, the `mem_replay` offline-cache replay, and the `reg_update` single registry.md CRUD) via a single `REGISTRY` dict (single source of truth for tool description, help, and SKILL.md). Single executable `dist/bin/awlab-ai-assistant.exe`; legacy 3-server files removed.
- **Server renamed `awlab-mcp` → `awlab-ai-assistant`** — MCP server name, executable (`dist/bin/awlab-ai-assistant.exe`), pip distribution (`awlab-ai-assistant`), the `awlab-mcp` skill (folder + generated SKILL.md), and the profile/build generator all renamed to match the new repo `awsomplak/awlab-ai-assistant` and brand AWLab AI-Assistant. The `~/.awlab-id/` config home and the AWLab-ID platform brand are unchanged.
- **Agentic orchestration**: `ctx_info mode="context"` assembles plan + next task + code + memory in one server-owned call and atomically regenerates `.ai/memory-bank/context.md`; `graph_query`/`graph_explain` return `related_memory` (code ↔ memory correlation).
- **Consolidated `task_update`**: multi-level dotted paths, transition validation with `valid_targets`, auto-create, atomic rollback, and executed/skipped/created trace.
- **Strict plan/task numbering**: phases and task paths are sequential positive integers only (no decimals/letters) — parsing depends on it.
- **`GRAPH_PARALLEL` config** (default off): sequential extraction is proven faster at realistic project scale and avoids the frozen-exe pool hang; opt in for very large corpora via `GRAPH_PARALLEL=1`.
- **Profiles compiled directly to `dist/profiles/`** (dropped the `assets/profiles/` intermediate).
- **Frozen-exe graph extraction** runs single-process sequential (`ProcessPoolExecutor` hangs in the onefile exe).
- **Rules 13 → 14** — the offline-cache rule is compiled into the cline / copilot / claude / hermes profiles.
- **Family project-id resolution is file-authoritative** — `family_member_id()` precedence is now `.ai/project-id` file > declared `project_id` > `<slug>-<dir>` (previously declared-first).

### Fixed
- **Frozen exe graph-build deadlock**: `to_json` no longer shells out to `git` (pure file read of `.git/HEAD`/refs) — the subprocess deadlock that hung the onefile exe is gone.
- **Path node resolution**: `any`/`path` global placeholders remapped to module-scoped `_py_any`/`_py_path`; dangling edges cleaned on merge.
- **`_find_node_id` ambiguity**: exact-match pass on function names (strip `()`).
- **Family declared-id lookup on Windows** — `family_member_project_ids()` now normalizes member-path keys via `Path.resolve()`, so forward-slash paths in a live `project-families.json` (`D:/Project/...`) no longer fall through to the derived `<slug>-<dir>` id when a declared id exists.

### Builds
- `build.094 → build.096` (`awlab-ai-assistant v3.0.1+build.096`) — family schema v2 + reconciliation/seeding, offline cache + `mem_replay`, and the path-normalization fix. 319 tests pass, lint + format + dead-code clean. Validated live on the EkaMira `eka-warehouse` family (1277 nodes / 1256 edges, tags `eka_warehouse::` + `eka-warehouse-backend::`).

## [3.0.0] - 2026-07-01

### Added
- **Per-Agent Compilation Pipeline**: Rules and skills are now compiled into per-agent profiles via `python scripts/run.py compile-rules`, each with format-optimized output:
  - **Cline**: Individual `.md` files (HTML comments preserved) → `~/Documents/Cline/Rules/`
  - **Copilot**: `.instructions.md` with YAML frontmatter + offset headings → `~/.copilot/instructions/`
  - **Claude Code**: Single `CLAUDE.md` monolith with heading anchors → `~/.claude/`
  - **Hermes Agent**: Rules as `SKILL.md` with `applyTo` frontmatter + all skills → `~/.hermes/skills/`
- **MCP Server Split**: Monolithic `agent-memory` server split into 3 separate MCP servers to work around Copilot's per-server tool visibility limit (~15 tools):
  - `awlab-mcp` — Utility & context tools (6 tools)
  - `awlab-plan` — Registry, task, and workflow tools (17 tools)
  - `awlab-memory` — Memory & context store tools (13 tools)
- **3 standalone executables**: `awlab-mcp.exe`, `awlab-plan.exe`, `awlab-memory.exe` built via PyInstaller
- **Separate FastMCP instances** in `registration_plan.py` and `registration_memory.py` with their own `@mcp.tool()` decorators
- **`run_server(mcp_instance, server_name)` factory** in `lifecycle.py` for shared server initialization
- **Link-rewriting utility** (`_rewrite_refs()`): Converts file-based rule references to heading anchors in compiled monoliths
- **`_strip_html_comments()`** and **`_offset_headings()`** helpers for per-agent content processing
- **`_copy_skills()`** shared helper to avoid duplication across compile functions
- **Hermes publish target** in `PUBLISH_MAP`: publishes skills + compiled rules to `~/.hermes/skills/`

### Changed
- **`cmd_compile_rules()`**: Refactored into per-agent output functions (`_compile_cline`, `_compile_copilot`, `_compile_claude`, `_compile_hermes`)
- **`PUBLISH_MAP`**: Each agent has dedicated publish paths — Cline+Copilot share `~/.agents/skills/`, Claude uses `~/.claude/skills/`, Hermes uses `~/.hermes/skills/`
- **Tool renames** to bypass Copilot's internal safety filter:
  - `util_get_environment` → `util_get_project_meta`
  - `mem_delete_entities` → `mem_archive_entities`
  - `mem_open_nodes` → `mem_fetch_node_details`
  - `mem_add_observations` → `mem_tag_entity`
  - `mem_create_relations` → `mem_relate`
- **All tool descriptions shortened** to single-line to fit within Copilot's prompt token budget
- **`scripts/run.py`**: Per-agent compilation pipeline; builds 3 binaries instead of 1
- **`pyproject.toml`**: Added `awlab-plan` and `awlab-memory` console script entry points
- **All rule files + skill files** updated to reference correct server names; skill cross-references changed to heading anchors
- **Directory copy & uninstall logic**: Updated to recursive `rglob("*")` for nested skill structures

### Removed
- **`hermes-config.json`**: No longer generated — Hermes uses skills natively
- **`skills` → `~/.agents/skills/`** shared publish target — replaced by per-agent skills paths

### Fixed
- **VS Code batch approval bug**: Added scripts to pre-approve MCP tools in VS Code's state database

## [2.2.0] - 2026-06-29

### Changed
- **Renamed CLI entry point**: `awlab-id-mcp` → `awlab-mcp` across all source files, build scripts, configs, and documentation. Executable now outputs as `awlab-mcp.exe` (Windows) / `awlab-mcp` (Linux/macOS).
- **Cross-platform build support**: `run.py build --target-os=windows|linux|macos|all` — builds for a specific OS or all platforms. Generates `.spec` files for non-host OS targets. Default (`auto`) builds for the current OS. (`scripts/run.py`)

### Added
- **Production/Development environment detection** — Auto-detects production mode (PyInstaller exe or `AWLAB_ENV=production`) vs development mode (source). Routes config, logs, and `.env` loading to appropriate paths:
  - Production: `~/.awlab-id/agent-memory/` (config, `.env`, `config.json`, logs)
  - Development: project root (current behavior)
- **Professional logger** — Millisecond timestamps, tool-scoped logging, auto-prune of logs >30 days, ERROR level outputs to both log file and stderr. (`helpers/logger.py`)
- **`.env` and `config.json` loading** — Loaded from production `~/.awlab-id/agent-memory/` or project root depending on environment. (`config.py`)
- **DB fallback path updated**: `~/.awlab-id/agent-memory/memory/memory.db` (was `~/.awlab-id/agent-memory/memory.db`). (`helpers/workspace.py`)

## [2.1.0] - 2026-06-28

### Added
- **Copilot Dual-Environment Support** — Full transformation of Cline workflows and rules for VS Code Copilot:
  - **Workflows → Skills**: 4 Cline workflows converted to Copilot skills at `~/.agents/skills/` (`plan-status`, `retrospective`, `switch-plan`, `test-flow`) with proper YAML frontmatter (`name`, `description`, `user-invocable`)
  - **Rules → Instructions**: 11 Cline rules converted to Copilot instructions at `~/.copilot/instructions/` (`00-meta` through `10-pattern-lifecycle`) as `.instructions.md` files with keyword-rich descriptions for Copilot's discovery system
  - **Skill Updates**: `plan-creator` and `extract-patterns` now detect Cline vs Copilot environment (`$ENV`) and adapt paths, references, and behaviors accordingly
  - **Memory Maintenance**: Generalized "Agent-Recall" references to "knowledge graph" for cross-platform compatibility

### Fixed
- **Hallucination Prevention** (`07-model-router.instructions.md`) — Restored full anti-hallucination safeguards: Anti-Malformed Tool Call Rules, Native Tool Priority with concrete examples, API Response Strictness protocol, and Universal Model Awareness section
- **Missing Protocols Restored** (`02-plan-artifacts.instructions.md`) — Re-added Uninitialized Recovery Protocol, Bug Report Protocol, retrospective auto-trigger, and archiving rules that were omitted during initial conversion
- **Priority Back-References** (`00-meta.instructions.md`) — Added full priority table mapping both Cline and Copilot filenames for every priority level
- **Per-Task Memory Update Rule** (`02-plan-artifacts.md`, `02-plan-artifacts.instructions.md`) — New rule: update memory via `add_observations` after EVERY single task completion (not just at phase completion) to prevent memory staleness in large-scale projects
- **Stale Tool Name** (`Cline/Rules/00-meta.md`) — Fixed `memory_search` → `search_nodes` (wrong knowledge graph tool name)
- **Test Flow Vague Instructions** (`test-flow/SKILL.md`) — Rewrote with full framework detection table (Jest, Vitest, Pytest, PHPUnit, Flutter, Cargo, Go) and dependency check step

### Changed
- **plan-creator skill**: `environment.md` generation is now conditional (Cline only); review queue path is conditional; ending references use correct file format per environment
- **Cline Rules expanded**: Added 3 new rule files — `08-project-id.md`, `09-user-patterns.md`, `10-pattern-lifecycle.md` (previously existed as skills/workflows, now formalized as rules)

## [2.0.5] - 2026-06-11

### Added
- **MCP Server package build & installation** (`pyproject.toml`, `mcp_server/`) — Added `[project.scripts]` entry point `awlab-mcp = "mcp_server.server:main"` so the server can be invoked as a CLI command after `pip install -e .`.
- **MCP Server documentation** (`README.md`, `mcp_server/README.md`) — Added mermaid architecture diagram showing the relationship between Cline Rules, agent-memory MCP, agent-recall, and the Python package. Added three configuration options (installed CLI, registered console script, and raw python module) in Cline's `cline_mcp_settings.json`.
- **MCP Server v1.1.0 features** (`mcp_server/`) — Workspace resolution fix with 5-layer fallback chain, `get_server_version` tool, `read_graph` tool, `delete_relations` tool. See `mcp_server/CHANGELOG.md` for details.

### Changed
- **Removed redundant wrapper files** — Deleted `awlab-id.mcp_server.cmd` and the `awlab_id/` wrapper package. The `mcp_server/` package is now the single source of truth for the MCP server implementation, with the `pyproject.toml` entry point and `pip install -e .` serving as the installation mechanism.
- **README.md** — Fully rewritten with professional MCP architecture diagrams, workspace resolution flowchart, quick-start guide, and comprehensive documentation of all 40+ server tools grouped by domain.

## [2.0.4] - 2026-05-19

### Added
- **Rules Compact Profile** (`Cline/portability/.clinerules`) — Single high-density `.clinerules` profile in `Cline/portability/` (achieving ~90% token reduction).
- **Permissive Q&A Exception** (`02-plan-artifacts.md`) — Refactored the Uninitialized Recovery Protocol to introduce a permissive exception: read-only, exploratory, and diagnostic actions completely bypass active plan locks and registry gates instantly, while code modifications remain strictly gated.
- **Continuous Phase Execution** (`02-plan-artifacts.md`) — Implemented continuous phase transitions, permitting the agent to bypass sequential phase gate halts and yield pauses when continuous execution is explicitly requested by the developer.

### Fixed
- **Obsolete Shell Date Mismatches Resolved** (`01-memory-bank.md`, `06-project-scanner.md`, `07-model-router.md`) — Removed all fragile, shell-based date comparison scripts for environment and cache staleness checks. Replaced with **Cognitive Date-Math** against system prompt metadata, and cleaned up obsolete exception rules in `07-model-router.md`.
- **Retrospective Status Alignment** (`02-plan-artifacts.md`, `retrospective.md`) — Formally added the retrospective status `🔄` (Retrospective/Reviewing) to the approved registry Status Values inside the core constraints, eliminating model parsing warnings.
- **Installer Integration** (`install.ps1`, `install.sh`) — Integrated the compiler script execution directly into the PowerShell and Bash installers, automating the deployment of the compiled profile to standard documents directory pathways.

## [2.0.3] - 2026-05-19

### Added
- **High-Speed Cloud Model Routing** (`07-model-router.md`) — Introduced dedicated awareness and optimization guidelines for high-speed cloud models (e.g. Gemini 3 Pro/Flash, GPT-4o-mini, Claude 3.5 Haiku, DeepSeek v4 Pro/Flash, and etc.), authorizing them to handle complex scopes eagerly while strictly maintaining native tool priority for maximum performance.
- **Archived Plans Visibility** (`plan-status.md`) — Added support to parse and display the total count of completed plans located in the archive table, preventing user confusion about deleted plans.

### Fixed
- **Registry Archive Overwriting** (`02-plan-artifacts.md`) — Updated the archiving protocol to overwrite existing plan records in the archive instead of filtering them as duplicates, preserving the latest completion summaries and timestamps for re-opened plans.
- **Dependency Deadlock Protections** (`02-plan-artifacts.md`) — Mandated that `→ depends: {exact task name}` must exactly match the name of another task in the same plan, preventing the agent from stalling on conceptual or unwritten dependencies.
- **Dual-Active Plan Paradox Resolved** (`02-plan-artifacts.md`) — Enforced that the active plan must be paused (`⏸️`) before reactivating a completed plan (`✅`) to `⏹️` due to a bug report, maintaining the single-active-plan constraint.
- **Archive Plan Restoration** (`switch-plan.md`) — Added a rule to restore archived plans back into the active registry (`registry.md`) and remove them from the archive (`registry_archive.md`) when switched back to active.
- **Unified Cache Thresholds** (`01-memory-bank.md`, `04-commands.md`, `05-environment.md`, `update-memory.md`) — Synchronized the environment check stale threshold to exactly 30 days across all rules, reference tables, and workflows (upgraded from 14 days).
- **Git Scanner Vulnerability Protection** (`06-project-scanner.md`) — Added silent command fallbacks when running Git operations, preventing shell crashes or infinite recovery loops in non-git repositories or fresh folders.
- **Scaffolding Registry Write Exception** (`02-plan-artifacts.md`) — Whitelisted `./.ai/artifacts/registry.md` as an exception in the Target Path Lock, permitting the plan-creator skill to register plans while maintaining robust path-traversal security.
- **Recovery Scaffolding Confirmations** (`SKILL.md`) — Expanded the plan-creator's strict trigger keywords to recognize standard recovery affirmations like `"yes"`, `"scaffold"`, and `"yes, scaffold"`, allowing seamless scaffolding initialization during recovery.

## [2.0.2] - 2026-05-19

### Fixed
- **Oversimplification Bugs Resolved** (`02-plan-artifacts.md`) — Reinstated critical instructional sub-bullets, execution steps, and markdown templates that were accidentally stripped, restoring the AI's ability to properly execute phases and format registry files.
- **Infinite Loop Skip Trap** (`02-plan-artifacts.md`) — Introduced the `[⏳]` (Deferred) marker to differentiate tasks skipped due to unmet dependencies from those permanently skipped (`[—]`), preventing infinite re-evaluation loops.
- **Dependency Cascade Fallback** (`02-plan-artifacts.md`) — Added a safety net to halt execution if a circular or future-phase dependency is encountered, preventing infinite looping at the end of a phase.
- **Dynamic Loading Budget Safety** (`01-memory-bank.md`) — Added a budget constraint note forcing the AI to prioritize files and defer loading when pre-load heuristics exceed the turn file budget limits.
- **Native Tool Priority Exception** (`07-model-router.md`) — Explicitly whitelisted PowerShell `New-TimeSpan` and Unix `date +%s` math under the native tool priority rule, resolving the paradox where the AI refused to run required file age checks.

## [2.0.1] - 2026-05-18

### Added
- **System Authority Framework** (`00-meta.md`) — establishes a strict hierarchy for resolving conflicts between rules and workflows. All rules now declare their authority.
- **Model Router** (`07-model-router.md`) — auto-classifies tasks as Simple (🟢), Medium (🟡), or Complex (🔴), enabling dynamic escalation for local LLM routing and token awareness.
- **Model-Adaptive Loading Modes** (`01-memory-bank.md`) — dynamic toggling between Compact (≤16K), Standard (16K-128K), and Full (≥128K) modes to prevent context window explosion on smaller models.
- **Quick Index & Relationship Detection** (`06-project-scanner.md`) — scanner now pre-maps core concepts and relationships by reading only the first 20 lines of imports, completely avoiding bulk `view_file` context explosions.
- **Cross-Session Learning Workflow** (`retrospective.md`) — triggered after a plan successfully completes, extracting reusable architectures and auto-updating plan templates with YAML metadata (`success_count`).
- **Case-Insensitive Lowercase UUIDs** (`02-plan-artifacts.md`, `SKILL.md`) — all plan UUIDs are now strictly generated as lowercase alphanumeric (`[a-z0-9]{8}`). This prevents case-insensitive filesystem collisions on Windows/macOS and simplifies developer CLI interaction.
- **Strict Linear Phase checklists** (`02-plan-artifacts.md`) — streamlined task checklists to enforce sequential execution inside phases. This removes complex non-linear topological sorting overhead and resolves potential "Dependency Cascade Failures" in complex plans.
- **Native IDE Tool Priority Protocol** (`07-model-router.md`) — mandates that agents prioritize native IDE/system tools (`view_file`, `replace_file_content`, `grep_search`, `write_to_file`) over raw terminal command scripts (`cat`, `sed`, `grep`, `echo`), accelerating file manipulation and avoiding terminal permission blocks.

### Changed
- **Task Status Update Bloat Fixed** (`02-plan-artifacts.md`) — explicitly allows batching of small, related task updates instead of saving `tasks.md` after every single checkbox, saving massive tokens.
- **Command Validation Silenced** (`05-environment.md`) — forces the command verification protocol to be executed silently in the AI's thought process rather than over-narrating in the chat window.
- **Dangling Token Thresholds Cleaned Up** (`04-commands.md`) — removed outdated context budget point threshold references from `summarize session`.
- **Install Scripts Upgraded** (`install.ps1`, `install.sh`) — updated headers to v2.0.1 and bumped verification check counts to 8 rules (`00-07`).
- **Plan Templates Documentation Added** (`README.md`) — resolved a documentation gap by adding a detailed **Plan Templates** section, describing all six pre-built skeletons and how keyword-matching operates in the `plan-creator` skill.

### Fixed
- **Hallucination Vectors Closed** — explicitly added fallback prompts to `brief.md` and `context.md` in `01-memory-bank.md` to prevent hallucinating content when projects lack a README.
- **Workflow Turn-Yielding Enforced** (`SKILL.md`, `retrospective.md`) — added rigid `STOP AND WAIT` directives after plan creation and retrospective requests, eliminating hallucinated user responses.
- **Circular Dependency Deadlock** (`02-plan-artifacts.md`) — added an explicit fallback for when tasks skip endlessly due to unmet `→ depends:` chains, and allowed skipped conditional tasks `[—]` to count as satisfied prerequisites.
- **Out-of-Order Dependency Loops Resolved** (`02-plan-artifacts.md`) — mandated that agents loop back and re-evaluate skipped tasks within a phase once their dependencies are met, preventing premature phase exits.
- **Task Failure Skip Trap Avoided** (`02-plan-artifacts.md`) — instructed agents to change failed tasks `[!]` to `[—]` if skipped, preventing blockers during final verification.
- **Template Path Ambiguity** (`SKILL.md`) — hardcoded the fallback global/local path structure for resolving templates to prevent agent looping.
- **Lazy-Load Table Protection** (`update-memory.md`) — added strict warning to read `decisions.md` before appending to protect markdown table formatting from corruption.
- **Bash Empty-Operand Age Check Crash** (`01-memory-bank.md`) — resolved the macOS/Linux age check syntax error when `environment.md` is missing on fresh workspaces (added robust fallback `|| echo 0` and file-existence check).
- **PowerShell File-Not-Found Exception** (`01-memory-bank.md`) — wrapped the Windows environment check in `Test-Path` to prevent terminating shell errors on uninitialized clean projects.
- **Git Branch Desynchronization Trap** (`.gitignore`) — adjusted `.gitignore` to allow committing planning documents, decisions, and patterns. This enables team collaboration and branch-synchronized memory states, eliminating architectural hallucinations on branch changes while maintaining machine-specific local privacy (`environment.md` and temporary command streams are still ignored).
- **Retrospective Turn-Yield Memory Loss** (`retrospective.md`) — fixed a state-machine loophole where blank AI agents lose their state after yielding the turn for feedback. Introduced a transition status `🔄` (Review/Retrospective) in the plan registry to persist the review state across turns.

## [2.0.0] - 2026-05-17

### Added

- **Smart Project Fingerprinting** (`06-project-scanner.md`) — deterministic detection tables for 30+ languages, frameworks, mobile platforms, monorepo tools, test frameworks, and CI/CD systems. Framework-aware scan targets eliminate wasted token budget on irrelevant directories.
- **Context Budget System** — turn-counting proxy (15/25/30 checkpoints) replaces unmeasurable "~70% capacity" heuristic. File-size budgets (30-80 lines per memory file) and Memory Compression Protocol prevent bloat.
- **Plan Templates** — 6 pre-built plan skeletons (`feature-crud`, `auth-flow`, `migration`, `refactor`, `bugfix`, `integration`) with keyword-based auto-matching in the plan-creator skill.
- **Task Dependencies** — `→ depends: Task N` syntax for execution ordering and `? if: condition` syntax for conditional tasks.
- **Decision Log** (`decisions.md`) — architectural decision log with Date/Decision/Alternatives/Rationale table format. 20-row cap with archive. Lazy-loaded.
- **Verification Protocol** — Auto-Verify Checklist, 4 verification levels (`[x]`, `[x✓]`, `[x!]`, `[!]`), and Phase Gate quality checks before phase completion.
- **Multi-Tool Portability** — adapter guides for Cursor, Windsurf, and GitHub Copilot in `Cline/portability/`.
- **Task failure handling** — `[!]` marker with immediate STOP and user prompt (Phase Execution Rule #6).
- **`[—]` skipped marker** — for conditional tasks that don't apply.
- **`start phase {N}` command** — explicit command for phase execution.
- **Install scripts** — `install.sh` (macOS/Linux) and `install.ps1` (Windows) for one-command installation. Idempotent, colored output, `--uninstall` / `-Uninstall` flag for cleanup.
- **Output Capture Workaround** (`05-environment.md`) — prevents blank command output in Cline by banning `Format-Table`/`Format-List`, requiring `Out-String` for long pipelines, and providing file-based fallback capture.

### Changed

- **Registry Integrity** — rewritten to clarify that automated rule/workflow/skill changes are expected; only manual ad-hoc edits discouraged.
- **Command Validation Protocol** (`05-environment.md`) — consolidated from 27 lines to 8 lines by removing duplicate ❌ examples that overlapped with Anti-Patterns section. Net reduction: 154 → 133 lines.
- **Shell Mismatch Recovery** — merged "Mid-Session Shell Change Detection" and "Update Trigger" into single section.
- **SKILL.md** — Steps 1, 3 compressed; Step 4 upgraded with scanner reference; Step 7 upgraded with template matching.
- **switch-plan.md** — lazy-loads `tasks.md` only; `plan.md` on-demand.
- **update-memory.md** — checklist now includes `environment.md` and `decisions.md`.
- **plan-status.md** — shows task marker legend in output (all 5 non-pending markers).
- **Phase Execution Rule #7** — `[x!]` (completed with warnings) now accepted for plan completion with acknowledgment gate: AI displays warnings summary, asks user to confirm.
- **README** — Quick Install section with scripts (recommended); manual commands in `<details>` fallback. Directory tree includes `install.sh`/`install.ps1`. Portability section clarifies guides are reference-only.

### Fixed

- Stale `/create-plan` reference in `plan-status.md` → now `create plan` (skill trigger).
- Duplicated `$?` in Bash exit code row → `$?` and `${PIPESTATUS[@]}`.
- Duplicate "This will:" text in README.md.
- Missing `start phase {N}` in Session Commands table.
- Missing orphan handling when `tasks.md` doesn't exist for active plan.
- Stale cross-reference `Context Management` → `Context Budget` in `04-commands.md`.
- Ambiguous `./templates/` path in SKILL.md → `this skill's templates/ directory`.
- Missing `[x!]` marker in `plan-status.md` legend and README inline marker list.
- README installation section missing template directory copy commands.
- README section numbering jump from `### 1.` to `### 3.` → fixed to sequential.

## [1.0.2] - 2026-05-17

### Added

- **Environment Detection Rule** (`Cline/Rules/05-environment.md`) — definitive authority on OS/shell detection before any command execution. Includes detection procedure, shell command translation table, anti-patterns by shell, and mid-session shell change detection.
- **`plan-creator/SKILL.md` improvements** — stronger activation triggers, clearer phase execution reference, and better task generation examples.
- **`.gitignore`** — excludes `.ai/` and `scripts/` directories from version control.
- **`update-memory.md` enhancements** — better structure for memory bank sync workflow.

### Changed

- **Memory bank rules** (`01-memory-bank.md`) — clarified auto-setup (directories only, no content population), refined lazy loading strategy, stricter security constraints on path traversal and secret copying.
- **Plan artifacts rules** (`02-plan-artifacts.md`) — registry integrity section added, external modification detection, orphan detection protocol.
- **Token strategies** (`03-token-strategies.md`) — stronger lazy-loading rules, context window monitoring thresholds, anti-patterns clarified.
- **Command reference** (`04-commands.md`) — updated to reference the new environment detection rule, clarified session commands.
- **README** — updated to reflect new files and environment detection capabilities.

## [1.0.1] - 2026-05-16

### Optimized

- **Removed `workflows/create-plan.md`** – its content was identical to `plan-creator/SKILL.md`, causing redundancy and potential confusion. Plan creation now exclusively uses the skill.
- **Centralized Phase Execution Rules** – now defined only in `Rules/02-plan-artifacts.md`. The skill and other workflows reference that single source.
- **Clarified `follow rules`** – loads only the active plan’s `tasks.md` at session start; memory bank files are loaded on-demand for maximum token savings.
- **Security constraints** added to `01-memory-bank.md`: path traversal prevention, no secret copying, content treated as data.
- **Stronger skill triggers** in `plan-creator/SKILL.md` to prevent false activations (e.g., from questions about plans).
- **Updated command mappings** in `04-commands.md`: `create plan` now directly activates the skill; removed `/create-plan` workflow entry.
- **Token strategies** refined: registry caching, avoidance of empty `notes.md`, explicit lazy-loading rules.
- **Directory structure** clarified: Rules and Workflows are now separate directories for easier installation and maintenance.
- **Documentation** (README) fully updated to reflect all changes.

## [1.0.0] - 2026-05-15

- Initial release with plan management, memory bank, phase‑by‑phase execution, and token saving strategies.
