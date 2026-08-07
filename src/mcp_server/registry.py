"""
Single source of truth for the consolidated action surface (action_call / action_help).

The ``REGISTRY`` dict drives EVERYTHING the agent sees:
- ``build_tool_description()``  → the ``action_call`` tool description (top-level help)
- ``build_help()``              → the ``action_help`` output
- ``build_skill_md()``          → the generated SKILL.md

Principles (see docs/REGISTRY_SCHEMA.md):
1. ONE source of truth — nothing else defines the action surface (no drift).
2. Server-owned orchestration — ``preconditions`` + ``pipeline`` guarantee the
   complete flow for one request; no partial execution.
3. Coarse complete actions — 36 partial tools → 16 actions (incl. ``graph_*``).
4. Reuse existing business logic as-is — handlers delegate to ``tools/*``.
5. Backward compatible — all 36 old tool names are ``aliases``.
6. Loud failure + transparent trace (``executed`` / ``skipped``).
"""

from __future__ import annotations

import asyncio
import json
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import helpers
from .config import settings
from .helpers.context_builder import materialize_context
from .helpers.graphify_bridge import (
    build_graph as _graph_build,
)
from .helpers.graphify_bridge import (
    ensure_fresh as _graph_ensure_fresh,
)
from .helpers.graphify_bridge import (
    explain_node as _graph_explain,
)
from .helpers.graphify_bridge import (
    graph_status as _graph_status,
)
from .helpers.graphify_bridge import (
    path_query as _graph_path,
)
from .helpers.graphify_bridge import (
    query_graph as _graph_query,
)
from .tools import context_tools, file_tools, memory_tools, plan_tools, utils_tools

# ══════════════════════════════════════════════════════════════════════════
# ── Schema constants & validation ─────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════

VALID_GROUPS = {"task", "plan", "memory", "context", "util", "workflow", "graph"}
_PARAM_TYPES = {"string", "integer", "boolean", "array", "object"}
_SPEC_KEYS = {
    "group",
    "summary",
    "doc",
    "handler",
    "params",
    "returns",
    "example",
    "preconditions",
    "pipeline",
    "mutates",
    "aliases",
}


def _validate_spec(name: str, spec: dict[str, Any]) -> None:
    """Validate an ActionSpec at import time — fail fast, never drift silently."""
    unknown = set(spec) - _SPEC_KEYS
    if unknown:
        raise ValueError(f"REGISTRY[{name}]: unknown keys {sorted(unknown)}")
    if spec["group"] not in VALID_GROUPS:
        raise ValueError(f"REGISTRY[{name}]: invalid group {spec['group']!r}")
    for key in ("group", "summary", "doc", "handler", "example"):
        if not spec.get(key):
            raise ValueError(f"REGISTRY[{name}]: missing required field {key!r}")
    if not isinstance(spec.get("params", {}), dict):
        raise ValueError(f"REGISTRY[{name}]: params must be a dict")
    for pname, pspec in spec.get("params", {}).items():
        if pspec.get("type") not in _PARAM_TYPES:
            raise ValueError(f"REGISTRY[{name}].params[{pname}]: bad type")
    if spec.get("aliases") is not None and not isinstance(spec["aliases"], list):
        raise ValueError(f"REGISTRY[{name}]: aliases must be a list")


# ══════════════════════════════════════════════════════════════════════════
# ── Thin adapters for merged verbs (delegate to existing logic — no rewrite)
# ══════════════════════════════════════════════════════════════════════════


async def _task_update(
    workspace_path: str,
    project_id: str | None = None,
    plan_uuid: str = "",
    content: str | None = None,
    updates: list[dict[str, str]] | None = None,
    format: str | None = None,
    phases: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Merge task_write_plan_tasks / task_batch_update / task_format_markdown."""
    if content is not None:
        return await plan_tools.write_plan_tasks(workspace_path=workspace_path, plan_uuid=plan_uuid, content=content)
    if updates is not None:
        return await plan_tools.batch_update_tasks(
            workspace_path=workspace_path,
            project_id=project_id,
            plan_uuid=plan_uuid,
            updates=updates,
        )
    if format is not None:
        return await utils_tools.format_tasks_as_markdown(
            workspace_path=workspace_path, plan_uuid=plan_uuid, phases=phases
        )
    return helpers.fail_obj(error="task_update: provide content, updates, or format")


async def _plan_status(
    workspace_path: str,
    project_id: str | None = None,
    plan_uuid: str = "",
    phase: int | None = None,
    format: str = "minimal",
) -> dict[str, Any]:
    """Merge reg_list_registry / reg_get_next_eligible_task / reg_check_plan_completable / reg_validate_phase_gate."""
    registry = await plan_tools.list_registry(workspace_path=workspace_path, project_id=project_id)
    result: dict[str, Any] = {"registry": registry}
    uuid = plan_uuid or ((registry.get("active") or [{}])[0].get("uuid"))
    if uuid:
        result["next_task"] = await plan_tools.get_next_eligible_task(
            workspace_path=workspace_path,
            project_id=project_id,
            plan_uuid=uuid,
            phase=phase,
        )
        result["completable"] = await plan_tools.check_plan_completable(workspace_path=workspace_path, plan_uuid=uuid)
        if phase is not None:
            result["phase_gate"] = await plan_tools.validate_phase_gate(
                workspace_path=workspace_path,
                project_id=project_id,
                plan_uuid=uuid,
                phase_num=phase,
            )
    return result


async def _plan_update(
    workspace_path: str,
    project_id: str | None = None,
    mode: str = "",
    plan_uuid: str = "",
    phase_number: int | None = None,
) -> dict[str, Any]:
    """Merge reg_switch_active_plan / reg_mark_phase_complete / reg_resolve_deferred_tasks."""
    if mode == "switch" or (not mode and plan_uuid and phase_number is None):
        return await plan_tools.switch_active_plan(workspace_path=workspace_path, project_id=project_id, uuid=plan_uuid)
    if mode == "mark_phase" or (not mode and phase_number is not None):
        if phase_number is None:
            return helpers.fail_obj(error="plan_update: phase_number required for mark_phase")
        return await plan_tools.mark_phase_complete(
            workspace_path=workspace_path, plan_uuid=plan_uuid, phase_num=phase_number
        )
    return await plan_tools.resolve_deferred_tasks(
        workspace_path=workspace_path, plan_uuid=plan_uuid, phase_number=phase_number
    )


async def _ctx_info(
    workspace_path: str,
    mode: str = "snapshot",
    filename: str = "environment.md",
    task_description: str = "",
    force_refresh: bool = False,
    project_id: str | None = None,
    query: str = "",
) -> dict[str, Any]:
    """Merge ctx_get_snapshot / ctx_read_memory_bank / ctx_scan_project / ctx_suggest_files.

    ``mode="context"`` assembles the full orchestration context in one
    server-owned call: active plan + next task + relevant code nodes + relevant
    memory. An optional ``query`` scopes code/memory relevance.
    """
    if mode == "memory_bank":
        return await file_tools.read_memory_bank(workspace_path=workspace_path, filename=filename)
    if mode == "scan":
        return await context_tools.scan_project(workspace_path=workspace_path, force_refresh=force_refresh)
    if mode == "suggest":
        return await context_tools.suggest_relevant_files(
            workspace_path=workspace_path, task_description=task_description
        )
    if mode == "context":
        return await _context_composite(workspace_path, project_id=project_id, query=query)
    return await context_tools.get_context_snapshot(workspace_path=workspace_path)


async def _memory_inventory(
    workspace_path: str,
    project_id: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Return a memory inventory (what is stored) instead of an empty search.

    A fresh agent cannot know what to search for, so ``ctx_info mode="context"``
    with no query must not return blank memory. This reads the knowledge graph
    and summarizes entities by type with observation counts — the agent can then
    ask targeted queries or call ``mem_read`` on specific entities.
    """
    try:
        graph = helpers.read_graph(workspace_path=workspace_path, project_id=project_id, limit=limit)
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": f"memory inventory unavailable: {e}"}

    entities = graph.get("entities") or []
    relations = graph.get("relations") or []

    by_type: dict[str, int] = {}
    summary: list[dict[str, Any]] = []
    for ent in entities:
        name = ent.get("name") or ""
        etype = ent.get("entityType") or ent.get("type") or "unknown"
        obs = ent.get("observations") or []
        by_type[etype] = by_type.get(etype, 0) + 1
        summary.append({"name": name, "entityType": etype, "observation_count": len(obs)})

    return {
        "success": True,
        "mode": "inventory",
        "total_entities": len(entities),
        "total_relations": len(relations),
        "by_type": by_type,
        "entities": summary[: limit],
    }


async def _context_composite(
    workspace_path: str,
    project_id: str | None = None,
    query: str = "",
) -> dict[str, Any]:
    """Assemble the orchestration context: plan + plan.md + notes + task + code + memory.

    Server-owned composite: the agent never assembles it piecemeal. plan.md +
    notes.md are parsed structurally (not raw), so approach, preferences,
    decisions, constraints and risks reach the agent in one call.
    """
    try:
        plan = await _plan_status(workspace_path=workspace_path, project_id=project_id, format="minimal")
    except Exception:  # noqa: BLE001 — degrade gracefully
        plan = {"success": False, "error": "plan status unavailable"}

    # Active plan UUID (from plan_status registry) for plan.md/notes.md.
    active_uuid = ""
    try:
        registry = (plan or {}).get("registry") if isinstance(plan, dict) else None
        active = (registry or {}).get("active") if isinstance(registry, dict) else None
        if isinstance(active, list) and active:
            active_uuid = (active[0] or {}).get("uuid", "")
    except Exception:  # noqa: BLE001
        active_uuid = ""

    plan_doc: dict[str, Any] = {"success": False, "error": "no plan.md"}
    notes_doc: dict[str, Any] = {"success": False, "error": "no notes.md"}
    if active_uuid:
        try:
            raw_plan = helpers.read_plan_md(workspace_path=workspace_path, uuid=active_uuid)
            if raw_plan.get("content"):
                plan_doc = helpers.parse_plan_md(raw_plan["content"])
        except Exception:  # noqa: BLE001
            plan_doc = {"success": False, "error": "plan.md unreadable"}
        try:
            raw_notes = helpers.read_notes_md(workspace_path=workspace_path, uuid=active_uuid)
            if raw_notes.get("content"):
                notes_doc = helpers.parse_notes_md(raw_notes["content"])
        except Exception:  # noqa: BLE001
            notes_doc = {"success": False, "error": "notes.md unreadable"}

    code: dict[str, Any] = {"success": True, "results": [], "related_memory": []}
    mem: dict[str, Any] = {"success": True, "results": []}
    if query:
        try:
            # _graph_query is SYNC (graphify bridge); _maybe_await handles both.
            code = await _maybe_await(
                _graph_query, workspace_path=workspace_path, query=query, limit=5, project_id=project_id
            )
        except Exception:  # noqa: BLE001
            code = {"success": False, "error": "graph query unavailable"}
        try:
            mem = await memory_tools.search_memory(
                workspace_path=workspace_path, project_id=project_id, query=query, limit=5
            )
        except Exception:  # noqa: BLE001
            mem = {"success": False, "error": "memory search unavailable"}
    else:
        # No query → the agent can't know what's stored yet. Return an INVENTORY
        # (entity names + types + counts) instead of blank, so discovery works.
        mem = await _memory_inventory(workspace_path=workspace_path, project_id=project_id)
        try:
            status = _graph_status(workspace_path=workspace_path)
            if isinstance(status, dict) and status.get("exists"):
                code = {"success": True, "mode": "graph_status", **status}
        except Exception:  # noqa: BLE001
            pass

    return {
        "success": True,
        "plan": plan if isinstance(plan, dict) else plan,
        "plan_doc": plan_doc,
        "notes_doc": notes_doc,
        "code": code if isinstance(code, dict) else code,
        "memory": mem if isinstance(mem, dict) else mem,
        "query": query,
        "context_md": materialize_context(
            workspace_path,
            plan=plan if isinstance(plan, dict) else None,
            code=code if isinstance(code, dict) else None,
            memory=mem if isinstance(mem, dict) else None,
            query=query,
            plan_doc=plan_doc,
            notes_doc=notes_doc,
        ),
    }


async def _util_info(
    mode: str = "info", phases: list[str] | None = None, dependencies: list[dict[str, str]] | None = None
) -> dict[str, Any]:
    """Merge util_get_version / util_get_project_meta / util_generate_mermaid."""
    if mode == "mermaid":
        return await utils_tools.generate_mermaid(phases=phases, dependencies=dependencies)
    return {"version": await utils_tools.get_server_version(), "environment": await utils_tools.get_environment()}


async def _mem_write(
    workspace_path: str,
    project_id: str | None = None,
    entities: list[dict[str, Any]] | None = None,
    observations: list[dict[str, Any]] | None = None,
    relations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Merge mem_create_entities / mem_tag_entity / mem_relate / mem_store.

    Ensures every entity referenced by ``observations`` exists (auto-creates in
    the current scope if missing — mem_store behaviour), so an observation never
    silently drops. ``create_entities`` is idempotent: re-creating an existing
    entity is a no-op (updated), never a duplicate.
    """
    result: dict[str, Any] = {"success": True, "created": [], "observations": [], "relations": []}
    if entities:
        result["created"] = helpers.create_entities(
            workspace_path=workspace_path, project_id=project_id, entities=entities
        )
    if observations:
        # Ensure all observation targets exist in the current scope (idempotent).
        ensure = [
            {"name": n, "entityType": "concept", "observations": []}
            for n in dict.fromkeys(o.get("entityName", "") for o in observations if o.get("entityName"))
        ]
        if ensure:
            result["created"] = helpers.create_entities(
                workspace_path=workspace_path, project_id=project_id, entities=ensure
            )
        result["observations"] = helpers.add_observations(
            workspace_path=workspace_path, project_id=project_id, observations=observations
        )
    if relations:
        result["relations"] = helpers.create_relations(
            workspace_path=workspace_path, project_id=project_id, relations=relations
        )
    return result


async def _mem_read(
    workspace_path: str, project_id: str | None = None, node: str = "", limit: int = 50
) -> dict[str, Any]:
    """Merge mem_fetch_node_details / mem_read_graph."""
    if node:
        return {
            "success": True,
            "nodes": helpers.open_nodes(workspace_path=workspace_path, project_id=project_id, names=[node]),
        }
    return {
        "success": True,
        "graph": helpers.read_graph(workspace_path=workspace_path, project_id=project_id, limit=limit),
    }


async def _mem_remove(
    workspace_path: str,
    project_id: str | None = None,
    names: list[str] | None = None,
    deletions: list[dict[str, Any]] | None = None,
    relations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Merge mem_archive_entities / mem_delete_observations / mem_delete_relations."""
    result: dict[str, Any] = {"success": True}
    if names:
        result["archived"] = helpers.delete_entities(workspace_path=workspace_path, project_id=project_id, names=names)
    if deletions:
        result["deleted_observations"] = helpers.delete_observations(workspace_path=workspace_path, deletions=deletions)
    if relations:
        result["deleted_relations"] = helpers.delete_relations(workspace_path=workspace_path, relations=relations)
    return result


async def _wf(
    workspace_path: str, action: str = "list", workflow_name: str = "", params: str | None = None
) -> dict[str, Any]:
    """Merge wf_list / wf_execute."""
    if action == "execute":
        parsed = json.loads(params) if params else None
        return await plan_tools.execute_workflow(
            workspace_path=workspace_path, workflow_name=workflow_name, params=parsed
        )
    return await plan_tools.list_workflows(workspace_path=workspace_path)


# ══════════════════════════════════════════════════════════════════════════
# ── REGISTRY — single source of truth ─────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════

REGISTRY: dict[str, dict[str, Any]] = {
    # ── Task ──────────────────────────────────────────────────────────────
    "task_read": {
        "group": "task",
        "summary": "Read a plan's tasks.md as structured/raw/minimal JSON.",
        "doc": "Parse tasks.md into structured, raw, or minimal JSON. Structured output "
        "includes the resolvable dotted path (phase.task.subtask) for every task.",
        "handler": plan_tools.read_plan_tasks,
        "params": {
            "workspace_path": {"type": "string", "required": True, "desc": "Absolute path to project root"},
            "plan_uuid": {
                "type": "string",
                "required": True,
                "pattern": r"^[a-z0-9]{8}$",
                "desc": "8-char lowercase UUID",
            },
            "format": {
                "type": "string",
                "enum": ["structured", "raw", "minimal"],
                "default": "structured",
                "desc": "Output shape",
            },
        },
        "returns": "{success, phases:[{phase_number, name, tasks:[{path, description, status, indent, subtasks}]}]}",
        "example": 'action_call(action="task_read", params={"plan_uuid": "mcptool1", "format": "structured"})',
        "preconditions": ["workspace_valid", "plan_uuid_valid", "tasks_file_exists"],
        "aliases": ["task_read_plan_tasks"],
    },
    "task_update": {
        "group": "task",
        "summary": "Create or update tasks.md / tasks (multi-level paths, atomic).",
        "doc": "If tasks.md is missing, create it (from `content` or a skeleton). If present, apply "
        "targeted `updates`. Task paths are multi-level dotted (phase.task.subtask). Transition "
        "validation is internal; illegal transitions return valid_targets. Auto-create a missing "
        "phase/task chain by including `description` in an update.",
        "handler": _task_update,
        "params": {
            "workspace_path": {"type": "string", "required": True, "desc": "Absolute path to project root"},
            "plan_uuid": {
                "type": "string",
                "required": True,
                "pattern": r"^[a-z0-9]{8}$",
                "desc": "8-char lowercase UUID",
            },
            "project_id": {"type": "string", "desc": "Optional project ID for agent-recall isolation"},
            "content": {"type": "string", "desc": "Full markdown to write (upsert whole file)"},
            "updates": {
                "type": "array",
                "items": {"type": "object"},
                "desc": "[{task_path, new_status} | {task_path, new_status, description}] — description auto-creates",
            },
            "format": {"type": "string", "enum": ["markdown"], "desc": "Format mode (uses phases)"},
            "phases": {"type": "array", "items": {"type": "object"}, "desc": "Phase dicts for format mode"},
        },
        "returns": (
            "{success, successful, executed, skipped, created, failed, db_synced, pre_mutation_state}"
        ),
        "example": (
            'action_call(action="task_update", params={"updates": [{"task_path": "1.2", "new_status": "[x]"}]})'
        ),
        "preconditions": ["workspace_valid", "plan_uuid_valid"],
        "mutates": True,
        "aliases": [
            "task_update_status",
            "task_batch_update",
            "task_write_plan_tasks",
            "task_validate_transition",
            "task_format_markdown",
        ],
    },
    # ── Plan ──────────────────────────────────────────────────────────────
    "plan_status": {
        "group": "plan",
        "summary": "Read plan/registry status: active plan, next task, completeness, phase gate.",
        "doc": "Composite read over the registry + tasks.md. Returns active plans, the next "
        "eligible task, whether the plan is completable, and (optionally) a phase-gate check.",
        "handler": _plan_status,
        "params": {
            "workspace_path": {"type": "string", "required": True, "desc": "Absolute path to project root"},
            "project_id": {"type": "string", "desc": "Optional project ID"},
            "plan_uuid": {
                "type": "string",
                "pattern": r"^[a-z0-9]{8}$",
                "desc": "Optional plan UUID (defaults to active)",
            },
            "phase": {"type": "integer", "desc": "Phase number to also gate-check"},
            "format": {"type": "string", "enum": ["minimal", "full"], "default": "minimal"},
        },
        "returns": "{registry, next_task, completable, phase_gate?}",
        "example": 'action_call(action="plan_status", params={"phase": 2})',
        "aliases": [
            "reg_list_registry",
            "reg_get_next_eligible_task",
            "reg_validate_phase_gate",
            "reg_check_plan_completable",
        ],
    },
    "plan_update": {
        "group": "plan",
        "summary": "Mutate plan/registry: switch active plan, mark phase complete, resolve deferred tasks.",
        "doc": "mode=switch | mark_phase | resolve. When mode omitted, infers: plan_uuid without "
        "phase_number → switch; phase_number → mark_phase; else resolve deferred.",
        "handler": _plan_update,
        "params": {
            "workspace_path": {"type": "string", "required": True, "desc": "Absolute path to project root"},
            "project_id": {"type": "string", "desc": "Optional project ID"},
            "mode": {
                "type": "string",
                "enum": ["switch", "mark_phase", "resolve"],
                "desc": "Operation (inferred if omitted)",
            },
            "plan_uuid": {"type": "string", "pattern": r"^[a-z0-9]{8}$", "desc": "Target plan UUID"},
            "phase_number": {"type": "integer", "desc": "Phase number (mark_phase)"},
        },
        "returns": "{success, ...}",
        "example": 'action_call(action="plan_update", params={"mode": "switch", "plan_uuid": "mcptool1"})',
        "preconditions": ["workspace_valid", "plan_uuid_valid"],
        "mutates": True,
        "aliases": ["reg_switch_active_plan", "reg_mark_phase_complete", "reg_resolve_deferred_tasks"],
    },
    # ── Workflow ──────────────────────────────────────────────────────────
    "wf": {
        "group": "workflow",
        "summary": "List or execute a workflow.",
        "doc": "action=list (default) lists available workflows; action=execute runs a named "
        "workflow with optional JSON params.",
        "handler": _wf,
        "params": {
            "workspace_path": {"type": "string", "required": True, "desc": "Absolute path to project root"},
            "action": {"type": "string", "enum": ["list", "execute"], "default": "list"},
            "workflow_name": {"type": "string", "desc": "Workflow filename without .md"},
            "params": {"type": "string", "desc": "Optional JSON string of workflow params"},
        },
        "returns": "{success, workflows|result}",
        "example": 'action_call(action="wf", params={"action": "execute", "workflow_name": "scan-project"})',
        "aliases": ["wf_execute", "wf_list"],
    },
    # ── Context ───────────────────────────────────────────────────────────
    "ctx_info": {
        "group": "context",
        "summary": "Read project context: snapshot, memory-bank, scan, suggestions, or orchestration context.",
        "doc": "mode=snapshot (default) → active plan + patterns + project id. mode=memory_bank → "
        "read .ai/memory-bank/environment.md. mode=scan → framework scan. mode=suggest → "
        "suggest files for a task. mode=context → full orchestration composite: "
        "{plan, next task, relevant code nodes, relevant memory} in one server-owned call "
        "(optional query scopes code/memory relevance).",
        "handler": _ctx_info,
        "params": {
            "workspace_path": {"type": "string", "required": True, "desc": "Absolute path to project root"},
            "mode": {
                "type": "string",
                "enum": ["snapshot", "memory_bank", "scan", "suggest", "context"],
                "default": "snapshot",
            },
            "filename": {"type": "string", "default": "environment.md", "desc": "memory_bank file"},
            "task_description": {"type": "string", "desc": "suggest mode"},
            "force_refresh": {"type": "boolean", "default": False, "desc": "scan mode — bypass cache"},
            "project_id": {"type": "string", "desc": "Optional project ID (context mode memory scoping)"},
            "query": {"type": "string", "desc": "Optional term (context mode) to scope code + memory relevance"},
        },
        "returns": (
            "{success, plan, code, memory, query, context_md} for mode=context; "
            "snapshot/memory_bank/scan/suggest results otherwise"
        ),
        "example": 'action_call(action="ctx_info")',
        "aliases": ["ctx_get_snapshot", "ctx_read_memory_bank", "ctx_scan_project", "ctx_suggest_files"],
    },
    # ── Utility ───────────────────────────────────────────────────────────
    "util_info": {
        "group": "util",
        "summary": "Server version / project metadata (or mermaid generation).",
        "doc": "mode=info (default) → version + environment metadata. mode=mermaid → generate a "
        "Mermaid flowchart from phases/dependencies.",
        "handler": _util_info,
        "params": {
            "mode": {"type": "string", "enum": ["info", "mermaid"], "default": "info"},
            "phases": {"type": "array", "items": {"type": "string"}, "desc": "Phase names (mermaid)"},
            "dependencies": {"type": "array", "items": {"type": "object"}, "desc": "[{from, to}] (mermaid)"},
        },
        "returns": "{version, environment} | {mermaid_code}",
        "example": 'action_call(action="util_info")',
        "aliases": ["util_get_version", "util_get_project_meta", "util_generate_mermaid"],
    },
    # ── Memory ────────────────────────────────────────────────────────────
    "mem_search": {
        "group": "memory",
        "summary": "Hybrid BM25+dense search over memory (optionally by entity type).",
        "doc": "Search the knowledge graph with hybrid BM25+dense ranking over name + "
        "observations. Set entity_type to filter by the exact entityType field — "
        "this is how you list patterns (entity_type='pattern'). With entity_type set "
        "and query empty, lists ALL entities of that type deterministically.",
        "handler": memory_tools.search_memory,
        "params": {
            "workspace_path": {"type": "string", "required": True, "desc": "Absolute path to project root"},
            "query": {"type": "string", "default": "", "desc": "Search query (empty allowed with entity_type)"},
            "project_id": {"type": "string", "desc": "Optional project scope"},
            "limit": {"type": "integer", "default": 10, "desc": "Max results"},
            "use_dense": {"type": "boolean", "default": False, "desc": "Enable BM25+dense re-rank"},
            "entity_type": {"type": "string", "desc": "Exact entityType filter (e.g. 'pattern')"},
        },
        "returns": "{success, data:[...], filtered_by?}",
        "example": 'action_call(action="mem_search", params={"query": "registry schema"})',
        "preconditions": ["workspace_valid"],
        "aliases": ["mem_list_patterns"],
    },
    "mem_write": {
        "group": "memory",
        "summary": "Create/tag entities, add observations, or relate entities.",
        "doc": "Pass entities, observations, and/or relations in one call. Observations "
        "auto-create their entity if missing.",
        "handler": _mem_write,
        "params": {
            "workspace_path": {"type": "string", "required": True, "desc": "Absolute path to project root"},
            "project_id": {"type": "string", "desc": "Optional project scope"},
            "entities": {"type": "array", "items": {"type": "object"}, "desc": "[{name, entityType, observations}]"},
            "observations": {"type": "array", "items": {"type": "object"}, "desc": "[{entityName, contents}]"},
            "relations": {"type": "array", "items": {"type": "object"}, "desc": "[{from, to, relationType}]"},
        },
        "returns": "{success, created, observations, relations}",
        "example": 'action_call(action="mem_write", params={"observations": [{"entityName": "A", "contents": ["x"]}]})',
        "preconditions": ["workspace_valid"],
        "mutates": True,
        "aliases": ["mem_create_entities", "mem_tag_entity", "mem_relate", "mem_store"],
    },
    "mem_read": {
        "group": "memory",
        "summary": "Read node details or the graph neighbourhood.",
        "doc": "If node given → fetch that node's details (with depth). Else read the graph neighbourhood up to limit.",
        "handler": _mem_read,
        "params": {
            "workspace_path": {"type": "string", "required": True, "desc": "Absolute path to project root"},
            "project_id": {"type": "string", "desc": "Optional project scope"},
            "node": {"type": "string", "desc": "Node name to fetch details for"},
            "limit": {"type": "integer", "default": 50, "desc": "Max graph nodes"},
        },
        "returns": "{success, node|graph}",
        "example": 'action_call(action="mem_read", params={"node": "MCPBridge"})',
        "preconditions": ["workspace_valid"],
        "aliases": ["mem_fetch_node_details", "mem_read_graph"],
    },
    "mem_remove": {
        "group": "memory",
        "summary": "Archive entities or delete observations/relations.",
        "doc": "names → archive entities; deletions → remove observations; relations → remove "
        "relations. Pass any combination in one call.",
        "handler": _mem_remove,
        "params": {
            "workspace_path": {"type": "string", "required": True, "desc": "Absolute path to project root"},
            "project_id": {"type": "string", "desc": "Optional project scope"},
            "names": {"type": "array", "items": {"type": "string"}, "desc": "Entities to archive"},
            "deletions": {"type": "array", "items": {"type": "object"}, "desc": "[{entityName, observations}]"},
            "relations": {"type": "array", "items": {"type": "object"}, "desc": "[{from, to, relationType}]"},
        },
        "returns": "{success, archived, deleted_observations, deleted_relations}",
        "example": 'action_call(action="mem_remove", params={"names": ["stale-entity"]})',
        "preconditions": ["workspace_valid"],
        "mutates": True,
        "aliases": ["mem_archive_entities", "mem_delete_observations", "mem_delete_relations"],
    },
    # ── Graph (code knowledge graph) ────────────────────────────────────
    "graph_build": {
        "group": "graph",
        "summary": "Build/update the code knowledge graph into .ai/codegraph/ (AST-only, no LLM).",
        "doc": "Builds a per-project structural graph (detect -> extract -> build -> cluster -> "
        "export graph.json + graph.html) into <root>/.ai/codegraph/ and writes a "
        ".build_state.json manifest for freshness. graph.html is for the user to open "
        "manually in a browser.",
        "handler": _graph_build,
        "params": {
            "workspace_path": {"type": "string", "required": True, "desc": "Absolute path to project root"},
            "root": {"type": "string", "desc": "Scan root (defaults to workspace_path)"},
            "include_html": {"type": "boolean", "default": True, "desc": "Also export graph.html"},
            "directed": {"type": "boolean", "default": False, "desc": "Directed graph"},
            "project_id": {"type": "string", "desc": "Optional project ID for feedback-memory scoping"},
        },
        "returns": (
            "{success, out_dir, nodes, edges, files, artifacts} — rebuild writes "
            "graphify_feedback memory obs when files changed"
        ),
        "example": 'action_call(action="graph_build", params={"workspace_path": "D:/Project/Foo"})',
        "preconditions": ["workspace_valid", "graph_dir_ready"],
        "pipeline": ["scan_source", "extract", "build_graph", "cluster", "export_html", "write_state"],
        "mutates": True,
    },
    "graph_status": {
        "group": "graph",
        "summary": "Report code-graph freshness (exists? stale? changed files).",
        "doc": "Report whether the per-project code graph exists and is fresh (source unchanged since last build).",
        "handler": _graph_status,
        "params": {
            "workspace_path": {"type": "string", "required": True, "desc": "Absolute path to project root"},
            "root": {"type": "string", "desc": "Scan root (defaults to workspace_path)"},
        },
        "returns": "{exists, fresh, built_at, nodes, edges, changed_files}",
        "example": 'action_call(action="graph_status", params={"workspace_path": "D:/Project/Foo"})',
        "preconditions": ["workspace_valid"],
    },
    "graph_query": {
        "group": "graph",
        "summary": "Search the code graph (labels / source files / types). Auto-freshens first.",
        "doc": (
            "Search graph nodes by label / source file / type (case-insensitive, ranked). "
            "The graph_fresh precondition rebuilds the graph first if source files changed."
        ),
        "handler": _graph_query,
        "params": {
            "workspace_path": {"type": "string", "required": True, "desc": "Absolute path to project root"},
            "query": {"type": "string", "required": True, "desc": "Search term"},
            "limit": {"type": "integer", "default": 10, "desc": "Max results"},
            "root": {"type": "string", "desc": "Scan root (defaults to workspace_path)"},
            "project_id": {"type": "string", "desc": "Optional project ID for related-memory scoping"},
        },
        "returns": "{success, count, results:[{id, label, type, source_file}], related_memory:[{name, observations}]}",
        "example": (
            'action_call(action="graph_query", params={"workspace_path": "D:/Project/Foo", "query": "registry"})'
        ),
        "preconditions": ["workspace_valid", "graph_fresh"],
    },
    "graph_path": {
        "group": "graph",
        "summary": "Shortest path between two graph nodes. Auto-freshens first.",
        "doc": (
            "Shortest path (BFS) between two nodes by label. "
            "The graph_fresh precondition rebuilds the graph first if source files changed."
        ),
        "handler": _graph_path,
        "params": {
            "workspace_path": {"type": "string", "required": True, "desc": "Absolute path to project root"},
            "a": {"type": "string", "required": True, "desc": "From node label"},
            "b": {"type": "string", "required": True, "desc": "To node label"},
            "root": {"type": "string", "desc": "Scan root (defaults to workspace_path)"},
        },
        "returns": "{success, path:[labels], hops}",
        "example": (
            'action_call(action="graph_path", '
            'params={"workspace_path": "D:/Project/Foo", "a": "action_call", "b": "registry"})'
        ),
        "preconditions": ["workspace_valid", "graph_fresh"],
    },
    "graph_explain": {
        "group": "graph",
        "summary": "Explain a graph node (details + direct neighbours). Auto-freshens first.",
        "doc": (
            "Explain a node: its details + direct neighbours with relation types. "
            "The graph_fresh precondition rebuilds the graph first if source files changed."
        ),
        "handler": _graph_explain,
        "params": {
            "workspace_path": {"type": "string", "required": True, "desc": "Absolute path to project root"},
            "node": {"type": "string", "required": True, "desc": "Node label"},
            "limit": {"type": "integer", "default": 30, "desc": "Max neighbours"},
            "root": {"type": "string", "desc": "Scan root (defaults to workspace_path)"},
            "project_id": {"type": "string", "desc": "Optional project ID for related-memory scoping"},
        },
        "returns": (
            "{success, node:{id, label, type, source_file, source_location}, "
            "neighbours:[{node, relation}], related_memory:[{name, observations}]}"
        ),
        "example": (
            'action_call(action="graph_explain", params={"workspace_path": "D:/Project/Foo", "node": "registry"})'
        ),
        "preconditions": ["workspace_valid", "graph_fresh"],
    },
}

# Validate every spec at import time.
for _name, _spec in REGISTRY.items():
    _validate_spec(_name, _spec)

# Backward-compat alias index: old tool name → canonical action.
_ALIAS_INDEX: dict[str, str] = {}
for _action, _spec in REGISTRY.items():
    for _alias in _spec.get("aliases", []):
        _ALIAS_INDEX[_alias] = _action

# ══════════════════════════════════════════════════════════════════════════
# ── PRECONDITIONS — idempotent, guaranteed steps ─────────────────────────
# ══════════════════════════════════════════════════════════════════════════
# Contract: async def (workspace_path, params, state) -> (ok, note, state_update)


async def _pre_workspace_valid(workspace_path: str, params: dict, state: dict) -> tuple[bool, str, dict, bool]:
    ok, _ = helpers.validate_workspace_path(workspace_path)
    return ok, "workspace path valid" if ok else "invalid workspace path", {}, False


async def _pre_plan_uuid_valid(workspace_path: str, params: dict, state: dict) -> tuple[bool, str, dict, bool]:
    uuid = params.get("plan_uuid", "")
    ok = bool(helpers.validate_uuid(uuid))
    return ok, "plan_uuid valid" if ok else "invalid plan_uuid", {}, False


async def _pre_tasks_file_exists(workspace_path: str, params: dict, state: dict) -> tuple[bool, str, dict, bool]:
    uuid = params.get("plan_uuid", "")
    path = settings.get_plan_tasks_path(workspace_path=workspace_path, plan_uuid=uuid)
    if path.is_file():
        return True, "tasks.md exists", {}, False
    return False, f"tasks.md not found for plan '{uuid}'", {}, False


async def _pre_graph_dir_ready(workspace_path: str, params: dict, state: dict) -> tuple[bool, str, dict, bool]:
    out = Path(workspace_path) / ".ai" / "codegraph"
    out.mkdir(parents=True, exist_ok=True)
    readme = out / "README.md"
    if not readme.exists():
        readme.write_text(
            "Generated by graph_build (graphifyy, AST-only). Safe to delete; "
            'rebuild with action_call(action="graph_build", params={"workspace_path": "..."}).\n',
            encoding="utf-8",
        )
    return True, "codegraph dir ready", {}, False


async def _pre_graph_fresh(workspace_path: str, params: dict, state: dict) -> tuple[bool, str, dict, bool]:
    result = _graph_ensure_fresh(workspace_path)
    ok = bool(result.get("success"))
    updated = int(result.get("updated", 0) or 0)
    note = f"graph fresh ({updated} update)" if ok else result.get("error", "graph_fresh failed")
    return ok, note, {"graph_fresh": result}, updated > 0


PRECONDITIONS: dict[str, Callable[..., Awaitable[tuple[bool, str, dict, bool]]]] = {
    "workspace_valid": _pre_workspace_valid,
    "plan_uuid_valid": _pre_plan_uuid_valid,
    "tasks_file_exists": _pre_tasks_file_exists,
    "graph_dir_ready": _pre_graph_dir_ready,
    "graph_fresh": _pre_graph_fresh,
}

# ══════════════════════════════════════════════════════════════════════════
# ── Orchestration primitives ─────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════


async def _maybe_await(fn: Callable, *args: Any, **kwargs: Any) -> Any:
    """Call fn, awaiting if it returns a coroutine (handles sync + async handlers)."""
    result = fn(*args, **kwargs)
    if asyncio.iscoroutine(result):
        return await result
    return result


async def run_preconditions(
    spec: dict[str, Any],
    workspace_path: str,
    params: dict[str, Any],
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Run preconditions in order (idempotent gates). Returns (state, executed, skipped).

    A precondition returns ``(ok, note, state_update, did_work)``. Pure validation gates
    (did_work=False) gate but perform no work → they appear in ``skipped`` (already
    satisfied). Work-preconditions (e.g. ``graph_fresh``) report
    ``did_work=True`` when they performed an update → they appear in ``executed``.
    """
    state: dict[str, Any] = {}
    executed: list[str] = []
    skipped: list[str] = []
    for pc_name in spec.get("preconditions", []):
        pc = PRECONDITIONS.get(pc_name)
        if pc is None:
            raise RuntimeError(f"Unknown precondition '{pc_name}' in REGISTRY")
        ok, note, update, did_work = await pc(workspace_path, params, state)
        state.update(update or {})
        if not ok:
            raise RuntimeError(f"precondition '{pc_name}' failed: {note}")
        if did_work:
            executed.append(pc_name)
        else:
            skipped.append(pc_name)
    return state, executed, skipped


async def run_pipeline(
    spec: dict[str, Any], workspace_path: str, params: dict[str, Any], state: dict[str, Any]
) -> list[str]:
    """Run ordered pipeline steps (each reports done). Fails loudly naming the step.

    Steps without an injected ``_step_<name>`` function are treated as handled by the
    handler itself (e.g. atomic actions like ``graph_build``) and skipped.
    """
    executed: list[str] = []
    for step in spec.get("pipeline", []):
        step_fn = params.get(f"_step_{step}")
        if step_fn is None:
            continue  # implemented by the handler (not injected separately)
        try:
            await _maybe_await(step_fn, workspace_path=workspace_path, params=params, state=state)
            executed.append(step)
        except Exception as e:  # noqa: BLE001 — loud, names the failing step
            raise RuntimeError(f"pipeline step '{step}' failed: {e}") from e
    return executed


# ══════════════════════════════════════════════════════════════════════════
# ── Resolution: exact → alias → fuzzy (did-you-mean) ─────────────────────
# ══════════════════════════════════════════════════════════════════════════


def resolve_action(action: str) -> tuple[dict[str, Any] | None, str, list[str]]:
    """Resolve an action name. Returns (spec, canonical_name, did_you_mean)."""
    action = (action or "").strip()
    if action in REGISTRY:
        return REGISTRY[action], action, []
    if action in _ALIAS_INDEX:
        canonical = _ALIAS_INDEX[action]
        return REGISTRY[canonical], canonical, []
    suggestions = sorted(
        REGISTRY.keys(),
        key=lambda a: SequenceMatcher(None, action.lower(), a).ratio(),
        reverse=True,
    )[:3]
    return None, action, [a for a in suggestions if SequenceMatcher(None, action.lower(), a).ratio() > 0.3]


# ══════════════════════════════════════════════════════════════════════════
# ── Param validation ─────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════


def validate_params(spec: dict[str, Any], params: dict[str, Any] | None) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Validate + default params against spec['params']. Returns (validated, errors)."""
    errors: list[dict[str, str]] = []
    validated: dict[str, Any] = {}
    spec_params = spec.get("params", {})
    params = params or {}

    for name, pspec in spec_params.items():
        value = params.get(name, pspec.get("default"))
        if value is None:
            if pspec.get("required"):
                errors.append({"param": name, "reason": "required but missing"})
            continue
        # Type check
        if pspec.get("type") == "string" and not isinstance(value, str):
            errors.append({"param": name, "reason": "expected string"})
        elif pspec.get("type") == "integer" and not isinstance(value, int):
            errors.append({"param": name, "reason": "expected integer"})
        elif pspec.get("type") == "boolean" and not isinstance(value, bool):
            errors.append({"param": name, "reason": "expected boolean"})
        elif pspec.get("type") == "array" and not isinstance(value, list):
            errors.append({"param": name, "reason": "expected array"})
        elif pspec.get("type") == "object" and not isinstance(value, dict):
            errors.append({"param": name, "reason": "expected object"})
        # Enum / pattern
        if pspec.get("enum") and value not in pspec["enum"]:
            errors.append({"param": name, "reason": f"must be one of {pspec['enum']}"})
        if pspec.get("pattern") and isinstance(value, str) and not __import__("re").match(pspec["pattern"], value):
            errors.append({"param": name, "reason": f"must match {pspec['pattern']}"})
        validated[name] = value
    return validated, errors


# ══════════════════════════════════════════════════════════════════════════
# ── Generation (single source of truth → no drift) ───────────────────────
# ══════════════════════════════════════════════════════════════════════════


def build_tool_description() -> str:
    """Top-level description for the action_call tool (generated from REGISTRY)."""
    lines = [
        "Dispatch an MCP action. Actions (group → name):",
    ]
    for group in sorted({s["group"] for s in REGISTRY.values()}):
        names = sorted(a for a, s in REGISTRY.items() if s["group"] == group)
        lines.append(f"- {group}: {', '.join(names)}")
    lines.append('Use action_call(action="help") or action_help for per-action usage.')
    return "\n".join(lines)


def build_help(action: str | None = None) -> str:
    """action_help output. action=None → grouped overview; else full spec."""
    if not action:
        out = ["# Available Actions (group → name — summary)", ""]
        for group in sorted({s["group"] for s in REGISTRY.values()}):
            out.append(f"## {group}")
            for name in sorted(a for a, s in REGISTRY.items() if s["group"] == group):
                spec = REGISTRY[name]
                out.append(f"- `{name}` — {spec['summary']}")
            out.append("")
        return "\n".join(out)

    spec, canonical, suggestions = resolve_action(action)
    if spec is None:
        lines = [f"Unknown action '{action}'.", "Valid actions:"]
        lines += [f"- {a}" for a in sorted(REGISTRY)]
        if suggestions:
            lines.append("Did you mean: " + ", ".join(suggestions) + "?")
        return "\n".join(lines)

    lines = [
        f"# {canonical}  (group: {spec['group']})",
        "",
        spec["doc"],
        "",
        "## Params",
    ]
    for name, pspec in spec["params"].items():
        req = "required" if pspec.get("required") else f"default={pspec.get('default')!r}"
        enum = f" enum={pspec['enum']}" if pspec.get("enum") else ""
        lines.append(f"- `{name}` ({pspec['type']}, {req}{enum}): {pspec.get('desc', '')}")
    lines += ["", f"## Example\n```\n{spec['example']}\n```"]
    if spec.get("preconditions"):
        lines += ["", "## Preconditions (auto-run, idempotent)", *(f"- `{p}`" for p in spec["preconditions"])]
    if spec.get("pipeline"):
        lines += ["", "## Pipeline (ordered)", *(f"- `{p}`" for p in spec["pipeline"])]
    lines += ["", "## Returns", spec["returns"]]
    return "\n".join(lines)


def build_skill_md() -> str:
    """Generate the SKILL.md content from REGISTRY (single source of truth)."""
    out = [
        "---",
        "name: awlab-mcp",
        "description: Dispatch consolidated MCP actions via action_call(action=...).",
        "---",
        "",
        "# awlab-mcp — Action Reference",
        "",
        "Call `action_call` with `action` + params. Server guarantees preconditions/pipeline;",
        "responses include `executed`/`skipped` traces. Use `action_help(action)` for details.",
        "",
    ]
    for group in sorted({s["group"] for s in REGISTRY.values()}):
        out.append(f"## {group}")
        for name in sorted(a for a, s in REGISTRY.items() if s["group"] == group):
            spec = REGISTRY[name]
            out.append(f"- **{name}** — {spec['summary']}")
            out.append(f"  - Params: {', '.join(spec['params'].keys())}")
            out.append(f"  - Example: `{spec['example']}`")
        out.append("")
    return "\n".join(out)
