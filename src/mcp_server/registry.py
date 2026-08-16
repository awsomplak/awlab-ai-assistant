"""
Single source of truth for the consolidated action surface (action_call / action_help).

The ``REGISTRY`` dict drives EVERYTHING the agent sees:
- ``build_tool_description()``  → the ``action_call`` tool description (top-level help)
- ``build_help()``              → the ``action_help`` output
- ``build_skill_md()``          → the generated SKILL.md

Principles (see docs/en/REGISTRY_SCHEMA.md):
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
import functools
import json
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import helpers
from .config import settings
from .helpers.context_builder import materialize_context
from .helpers.file_utils import read_file_safe, write_file_safe
from .helpers.graphify_bridge import (
    ensure_fresh as _graph_ensure_fresh,
)
from .helpers.graphify_bridge import (
    explain_node as _graph_explain,
)
from .helpers.graphify_bridge import (
    graph_build_action as _graph_build,
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
from .tools.plan_tools.io import (
    append_pending as _pending_append,
)
from .tools.plan_tools.io import (
    pending_path as _pending_path,
)
from .tools.plan_tools.io import (
    read_pending as _pending_read,
)
from .tools.plan_tools.io import (
    replace_pending as _pending_replace,
)
from .tools.plan_tools.io import (
    sync_to_agent_recall as _pending_sync,
)

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
    """Merge reg_switch_active_plan / reg_mark_phase_complete / reg_resolve_deferred_tasks.

    Also routes ``mode="retrospective"`` (extract + store learned patterns) and
    AUTO-TRIGGERS the retrospective when the LAST phase completes — the agent
    grows with the user (patterns land in the dedicated user-patterns store).
    """
    if mode == "retrospective":
        if not plan_uuid:
            return helpers.fail_obj(error="plan_update: plan_uuid required for retrospective")
        return await plan_tools.generate_retrospective_summary(
            workspace_path=workspace_path, project_id=project_id, plan_uuid=plan_uuid
        )
    if mode == "switch" or (not mode and plan_uuid and phase_number is None):
        return await plan_tools.switch_active_plan(workspace_path=workspace_path, project_id=project_id, uuid=plan_uuid)
    if mode == "mark_phase" or (not mode and phase_number is not None):
        if phase_number is None:
            return helpers.fail_obj(error="plan_update: phase_number required for mark_phase")
        result = await plan_tools.mark_phase_complete(
            workspace_path=workspace_path, plan_uuid=plan_uuid, phase_num=phase_number
        )
        # Auto-learn: when the LAST phase completes, run the retrospective so the
        # agent grows with the user (best-effort — never breaks the response).
        if isinstance(result, dict) and result.get("success") and result.get("next_phase") is None:
            try:
                retro = await plan_tools.generate_retrospective_summary(
                    workspace_path=workspace_path, project_id=project_id, plan_uuid=plan_uuid
                )
                result["auto_learned"] = {
                    "patterns_extracted": retro.get("patterns_extracted", 0),
                    "stored_patterns": (retro.get("observations") or {}).get("stored_patterns", []),
                }
            except Exception:  # noqa: BLE001 — best-effort auto-learning
                pass
        return result
    return await plan_tools.resolve_deferred_tasks(
        workspace_path=workspace_path, plan_uuid=plan_uuid, phase_number=phase_number
    )


async def _reg_update(
    workspace_path: str,
    project_id: str | None = None,
    type: str = "",
    summary: str = "",
    uuid: str = "",
    status: str = "",
    confirmed: bool = False,
) -> dict[str, Any]:
    """Single registry.md CRUD: create / update status / delete a plan row.

    - ``create``: server generates the UUID (Active, ⏹️) — agent saves tokens.
    - ``update``: move the row to the correct table by status
      (active|paused|complete), refresh ``Date``, never touch ``Created At``;
      optional ``summary``.
    - ``delete``: remove the row — REQUIRES ``confirmed=true`` (strict user
      approval; without it the server refuses).
    """
    if type == "create":
        return await plan_tools.create_registry_entry(
            workspace_path=workspace_path, project_id=project_id, summary=summary
        )
    if type == "update":
        if not uuid or not status:
            return helpers.fail_obj(error="reg_update: uuid + status (active|paused|complete) required for update")
        return await plan_tools.update_registry_status(
            workspace_path=workspace_path, project_id=project_id, uuid=uuid, status=status, summary=summary or None
        )
    if type == "delete":
        if not uuid:
            return helpers.fail_obj(error="reg_update: uuid required for delete")
        if not confirmed:
            return helpers.fail_obj(
                error=(
                    "reg_update: deletion requires explicit user approval — "
                    "ask the user, then call again with confirmed=true"
                ),
                needs_approval=True,
                uuid=uuid,
            )
        return await plan_tools.delete_registry_entry(workspace_path=workspace_path, project_id=project_id, uuid=uuid)
    return helpers.fail_obj(error="reg_update: type must be create, update, or delete")


async def _project_id_check(
    workspace_path: str,
    project_id: str | None = None,
    force_regenerate: bool = False,
) -> dict[str, Any]:
    """Check the project-id; auto-create it if missing (idempotent).

    Reads ``.ai/project-id``; if missing (or force_regenerate), derives the
    sanitized directory-name slug (rule 08) and writes it. The agent calls this
    on FIRST response, BEFORE any mem_*/plan op, so memory isolation never falls
    through to the user-wide global DB (``~/.awlab-id/agent-memory/memory/memory.db``).
    """
    valid, err = helpers.validate_workspace_path(workspace_path)
    if not valid:
        return helpers.fail_obj(error=err or "invalid workspace_path")

    existing = settings.get_project_id(workspace_path)

    if existing and not force_regenerate:
        return {
            "success": True,
            "project_id": existing,
            "action": "check",
            "created": False,
            "path": str(settings.get_project_id_path(workspace_path)),
        }

    # Auto-create: sanitized dir-name slug (rule 08 bootstrap, server-side).
    root_name = settings._resolve_workspace(workspace_path).name
    slug = re.sub(r"[^a-z0-9_]+", "_", root_name.lower()).strip("_") or "project"
    pid_path = settings.get_project_id_path(workspace_path)
    ok = write_file_safe(pid_path, slug + "\n")
    if not ok:
        return helpers.fail_obj(error=f"project_id: failed to write {pid_path}")

    return {
        "success": True,
        "project_id": slug,
        "action": "create",
        "created": True,
        "path": str(pid_path),
        "note": "project-id auto-generated (was missing) — memory now project-isolated",
    }


async def _plan_doc(
    workspace_path: str,
    plan_uuid: str = "",
    doc: str = "plan",
    mode: str = "read",
    content: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Read / create / update / delete a plan's plan.md or notes.md directly.

    The agent passes the FULL content (no template, no IDE compare-changes) and
    reviews the complete result — not a diff. Modes: read (default) | write |
    delete. doc: plan (default) | notes.
    """
    if doc not in ("plan", "notes"):
        return helpers.fail_obj(error="plan_doc: doc must be 'plan' or 'notes'")
    if mode not in ("read", "write", "delete"):
        return helpers.fail_obj(error="plan_doc: mode must be read, write, or delete")
    if not plan_uuid:
        return helpers.fail_obj(error="plan_doc: plan_uuid required")

    path = (
        settings.get_plan_path(workspace_path=workspace_path, plan_uuid=plan_uuid)
        if doc == "plan"
        else settings.get_plan_dir(workspace_path=workspace_path, plan_uuid=plan_uuid) / "notes.md"
    )

    if mode == "read":
        content = read_file_safe(path)
        if content is None:
            return helpers.fail_obj(error=f"plan_doc: {doc}.md not found for {plan_uuid}")
        return {"success": True, "doc": doc, "mode": "read", "content": content, "path": str(path)}

    if mode == "delete":
        try:
            if path.exists():
                path.unlink()
            return {"success": True, "doc": doc, "mode": "delete", "path": str(path)}
        except OSError as e:
            return helpers.fail_obj(error=f"plan_doc: delete failed: {e}")

    # write / create
    if content is None:
        return helpers.fail_obj(error="plan_doc: content required for write")
    ok = write_file_safe(path, content)
    if not ok:
        return helpers.fail_obj(error=f"plan_doc: write failed for {path}")
    return {"success": True, "doc": doc, "mode": "write", "path": str(path)}


async def _mem_observe(
    workspace_path: str,
    project_id: str | None = None,
    observations: list[dict[str, Any]] | None = None,
    stack: str = "any",
) -> dict[str, Any]:
    """Record user-pattern evidence into the observation store (baking input).

    Agent-relayed observations: chat/behavior signals (explicit statements,
    corrections, repeated commands) that the agent noticed. The baking pipeline
    later keys → counts → measures consistency → computes confidence. Dedup/
    delta-guarded (fingerprint), so re-recording the same signal is a no-op.
    """
    from .helpers.observation_store import append_observations

    if not observations:
        return helpers.fail_obj(error="mem_observe: observations required")

    records = []
    for o in observations:
        if not isinstance(o, dict):
            continue
        source = str(o.get("source") or "behavioral")
        records.append(
            {
                "signature": str(o.get("signature") or ""),
                "value": str(o.get("value") or ""),
                "source": source,
                "stack": str(o.get("stack") or stack),
                "project": project_id or "",
                "context": str(o.get("context") or ""),
            }
        )

    res = append_observations(workspace_path=workspace_path, records=records)
    if not res.get("success"):
        return helpers.fail_obj(error=f"mem_observe: {res.get('error', 'write failed')}")
    return {
        "success": True,
        "store": "observations.jsonl",
        "appended": res.get("appended", 0),
        "skipped_duplicates": res.get("skipped_duplicates", 0),
        "skipped_invalid": res.get("skipped_invalid", 0),
    }


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
    store: str = "project",
) -> dict[str, Any]:
    """Return a memory inventory (what is stored) instead of an empty search.

    A fresh agent cannot know what to search for, so ``ctx_info mode="context"``
    with no query must not return blank memory. This reads the knowledge graph
    and summarizes entities by type with observation counts — the agent can then
    ask targeted queries or call ``mem_read`` on specific entities.
    """
    patterns, family = helpers.store_target(store)
    try:
        graph = helpers.read_graph(
            workspace_path=workspace_path, project_id=project_id, limit=limit, patterns=patterns, family=family
        )
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
        "store": store,
        "total_entities": len(entities),
        "total_relations": len(relations),
        "by_type": by_type,
        "entities": summary[:limit],
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

    # Pattern delivery (Phase 5): inject stack-scoped baked patterns + tell-once
    # candidates (marking them delivered in the same read).
    baked_patterns: list[dict[str, Any]] = []
    pattern_candidates: list[dict[str, Any]] = []
    try:
        from .helpers.baking import deliver_candidates, detect_stack, read_baked, scope_candidates

        baked_patterns = scope_candidates(
            read_baked(workspace_path).get("candidates") or [], detect_stack(workspace_path)
        )
        pattern_candidates = deliver_candidates(workspace_path).get("pattern_candidates") or []
    except Exception:  # noqa: BLE001 — delivery must never break the composite
        pass

    return {
        "success": True,
        "plan": plan if isinstance(plan, dict) else plan,
        "plan_doc": plan_doc,
        "notes_doc": notes_doc,
        "code": code if isinstance(code, dict) else code,
        "memory": mem if isinstance(mem, dict) else mem,
        "patterns": baked_patterns,
        "pattern_candidates": pattern_candidates,
        "query": query,
        "context_md": materialize_context(
            workspace_path,
            plan=plan if isinstance(plan, dict) else None,
            code=code if isinstance(code, dict) else None,
            memory=mem if isinstance(mem, dict) else None,
            query=query,
            plan_doc=plan_doc,
            notes_doc=notes_doc,
            patterns=baked_patterns,
            pattern_candidates=pattern_candidates,
        ),
    }


async def _util_info(
    mode: str = "info", phases: list[str] | None = None, dependencies: list[dict[str, str]] | None = None
) -> dict[str, Any]:
    """Merge util_get_version / util_get_project_meta / util_generate_mermaid."""
    if mode == "mermaid":
        return await utils_tools.generate_mermaid(phases=phases, dependencies=dependencies)
    return {"version": await utils_tools.get_server_version(), "environment": await utils_tools.get_environment()}


# ── Offline cache (MCP-down / store-down resilience) ───────────────────────


def _offline_cached(action: str):
    """Queue a failed mutation to the offline cache (pending.jsonl) instead of dropping it.

    Wraps a mutation handler: on any exception it appends the original params
    (as a JSONL entry) to the offline cache and returns a loud error with
    ``queued``/``pending_path``. ``mem_replay`` re-applies it once the store is
    back. The raw function stays reachable via ``__wrapped__`` (replay uses it
    so a re-queued failure can never re-queue itself).
    """

    def deco(fn: Callable[..., Awaitable[dict[str, Any]]]) -> Callable[..., Awaitable[dict[str, Any]]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
            try:
                return await fn(*args, **kwargs)
            except Exception as e:  # noqa: BLE001 — queue-and-surface, never drop
                entry: dict[str, Any] = {"type": action}
                for k, v in kwargs.items():
                    if v is not None:
                        entry[k] = v
                wp = kwargs.get("workspace_path", "")
                queued = _pending_append(workspace_path=wp, entry=entry)
                return helpers.fail_obj(
                    error=f"{action} failed: {e}",
                    queued=queued,
                    pending_path=str(_pending_path(wp)),
                )

        return wrapper

    return deco


@_offline_cached("mem_write")
async def _mem_write(
    workspace_path: str,
    project_id: str | None = None,
    entities: list[dict[str, Any]] | None = None,
    observations: list[dict[str, Any]] | None = None,
    relations: list[dict[str, Any]] | None = None,
    store: str = "project",
) -> dict[str, Any]:
    """Merge mem_create_entities / mem_tag_entity / mem_relate / mem_store.

    Ensures every entity referenced by ``observations`` AND ``relations`` exists
    — REUSING an existing same-named entity (any type) instead of always
    auto-creating a new one, so no empty duplicate entities are spawned (the
    auto-create used to hardcode ``entityType: "concept"`` and ``create_entities``
    matches on ``(name, type)``, which created ``X :: concept`` beside ``X :: feature``).
    """
    patterns, family = helpers.store_target(store)
    result: dict[str, Any] = {
        "success": True,
        "store": store,
        "created": {"created": 0, "updated": 0, "blocked": []},
        "observations": [],
        "relations": [],
    }
    # Aggregate explicit + auto-ensured entity writes into one created summary.
    created_agg: dict[str, Any] = {"created": 0, "updated": 0, "blocked": []}
    if entities:
        r0 = helpers.create_entities(
            workspace_path=workspace_path,
            project_id=project_id,
            entities=entities,
            patterns=patterns,
            family=family,
        )
        created_agg["created"] += int(r0.get("created", 0))
        created_agg["updated"] += int(r0.get("updated", 0))
        created_agg["blocked"] += list(r0.get("blocked", []))

    # Collect every name referenced by observations and relations so the store
    # is never polluted with empty duplicates.
    referenced: list[str] = []
    if observations:
        referenced += [o.get("entityName", "") for o in observations if o.get("entityName")]
    if relations:
        for r in relations:
            referenced += [r.get("from", ""), r.get("to", "")]
    referenced = list(dict.fromkeys(n for n in referenced if n))
    reused: list[str] = []
    if referenced:
        ensured = helpers.ensure_entities(
            workspace_path=workspace_path,
            project_id=project_id,
            names=referenced,
            entity_type="concept",
            patterns=patterns,
            family=family,
        )
        inner = ensured.get("created")
        if isinstance(inner, dict):
            created_agg["created"] += int(inner.get("created", 0))
            created_agg["updated"] += int(inner.get("updated", 0))
            created_agg["blocked"] += list(inner.get("blocked", []))
        created_agg["blocked"] += list(ensured.get("blocked", []))
        reused = ensured.get("reused", [])
    result["created"] = created_agg
    if reused:
        result["reused"] = reused

    if observations:
        result["observations"] = helpers.add_observations(
            workspace_path=workspace_path,
            project_id=project_id,
            observations=observations,
            patterns=patterns,
            family=family,
        )
    if relations:
        result["relations"] = helpers.create_relations(
            workspace_path=workspace_path,
            project_id=project_id,
            relations=relations,
            patterns=patterns,
            family=family,
        )
    return result


async def _mem_read(
    workspace_path: str,
    project_id: str | None = None,
    node: str = "",
    limit: int = 50,
    store: str = "project",
) -> dict[str, Any]:
    """Merge mem_fetch_node_details / mem_read_graph."""
    patterns, family = helpers.store_target(store)
    if node:
        return {
            "success": True,
            "nodes": helpers.open_nodes(
                workspace_path=workspace_path, project_id=project_id, names=[node], patterns=patterns, family=family
            ),
            "store": store,
        }
    return {
        "success": True,
        "graph": helpers.read_graph(
            workspace_path=workspace_path, project_id=project_id, limit=limit, patterns=patterns, family=family
        ),
        "store": store,
    }


@_offline_cached("mem_remove")
async def _mem_remove(
    workspace_path: str,
    project_id: str | None = None,
    names: list[str] | None = None,
    entities: list[dict[str, Any]] | None = None,
    deletions: list[dict[str, Any]] | None = None,
    relations: list[dict[str, Any]] | None = None,
    store: str = "project",
) -> dict[str, Any]:
    """Merge mem_archive_entities / mem_delete_observations / mem_delete_relations.

    Type-safe archiving: bare ``names`` are refused when ambiguous (same name,
    multiple entityTypes) with a candidate list; ``entities=[{name, entityType}]``
    specs archive exactly the right one — never the data-bearing entity.
    ``store="patterns"`` routes to the dedicated user-patterns store.
    """
    patterns, family = helpers.store_target(store)
    result: dict[str, Any] = {"success": True, "store": store}
    if names or entities:
        result["archived"] = helpers.delete_entities(
            workspace_path=workspace_path,
            project_id=project_id,
            names=names,
            entities=entities,
            patterns=patterns,
            family=family,
        )
    if deletions:
        result["deleted_observations"] = helpers.delete_observations(
            workspace_path=workspace_path, deletions=deletions, patterns=patterns, family=family
        )
    if relations:
        result["deleted_relations"] = helpers.delete_relations(
            workspace_path=workspace_path, relations=relations, patterns=patterns, family=family
        )
    return result


async def _mem_dedupe(
    workspace_path: str,
    project_id: str | None = None,
    name: str = "",
    dry_run: bool = True,
    store: str = "project",
) -> dict[str, Any]:
    """Dedupe adapter — merge same-named entities (see helpers.dedupe_entities).

    dry_run=True (default) returns the plan without mutating.
    ``store="patterns"`` routes to the dedicated user-patterns store.
    """
    patterns, family = helpers.store_target(store)
    return helpers.dedupe_entities(
        workspace_path=workspace_path,
        project_id=project_id,
        name=name,
        dry_run=dry_run,
        patterns=patterns,
        family=family,
    )


async def _mem_replay(
    workspace_path: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Drain the offline cache (pending.jsonl): re-apply queued mutations.

    Each entry is dispatched by ``type``: ``mem_write``/``mem_remove`` re-run
    the raw store operation; ``update_task_status`` re-runs the task update;
    ``sync_plan_progress`` re-runs the agent-recall DB sync. Successful entries
    are removed from the queue; failed ones are kept for a later retry.
    ``dry_run=True`` previews the queue without applying.
    """
    entries = _pending_read(workspace_path)
    if dry_run or not entries:
        return {
            "success": True,
            "dry_run": bool(dry_run),
            "count": len(entries),
            "pending": entries,
            "pending_path": str(_pending_path(workspace_path)),
        }

    succeeded: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    remaining: list[dict[str, Any]] = []
    raw_write = getattr(_mem_write, "__wrapped__", _mem_write)
    raw_remove = getattr(_mem_remove, "__wrapped__", _mem_remove)
    for idx, entry in enumerate(entries):
        etype = str(entry.get("type", ""))
        # ``type`` is queue metadata — never part of the handler's params.
        clean = {k: v for k, v in entry.items() if k != "type"}
        try:
            if etype == "mem_write":
                await raw_write(**clean)
            elif etype == "mem_remove":
                await raw_remove(**clean)
            elif etype == "update_task_status":
                await plan_tools.update_task_status(
                    workspace_path=workspace_path,
                    plan_uuid=entry.get("plan_uuid", ""),
                    task_path=entry.get("task_path", ""),
                    new_status=entry.get("new_status", ""),
                )
            elif etype == "sync_plan_progress":
                _pending_sync(
                    workspace_path=workspace_path,
                    plan_uuid=entry.get("plan_uuid", ""),
                    updates=entry.get("updates"),
                )
            else:
                raise ValueError(f"unknown pending entry type '{etype}'")
            succeeded.append({"index": idx, "type": etype})
        except Exception as e:  # noqa: BLE001 — keep the entry for a later retry
            failed.append({"index": idx, "type": etype, "error": str(e)})
            remaining.append(entry)
    _pending_replace(workspace_path, remaining)
    return {
        "success": True,
        "processed": len(succeeded),
        "succeeded": succeeded,
        "failed": failed,
        "pending_left": len(remaining),
        "pending_path": str(_pending_path(workspace_path)),
    }


async def _wf(
    workspace_path: str = "",
    action: str = "list",
    workflow_name: str = "",
    params: str | None = None,
    workflows_dir: str | None = None,
) -> dict[str, Any]:
    """Merge wf_list / wf_execute. Workflows are workspace-free; workspace_path optional."""
    wf_dir = Path(workflows_dir) if workflows_dir else None
    ws = workspace_path or None
    if action == "execute":
        parsed = json.loads(params) if params else None
        return await plan_tools.execute_workflow(
            workspace_path=ws, workflow_name=workflow_name, params=parsed, workflows_dir=wf_dir
        )
    return await plan_tools.list_workflows(workspace_path=ws, workflows_dir=wf_dir)


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
        "returns": ("{success, successful, executed, skipped, created, failed, db_synced, pre_mutation_state}"),
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
        "summary": "Mutate plan/registry: switch, mark phase complete, resolve deferred, run retrospective.",
        "doc": "mode=switch | mark_phase | resolve | retrospective. When mode omitted, infers: "
        "plan_uuid without phase_number → switch; phase_number → mark_phase; else resolve "
        "deferred. mode=retrospective extracts + stores learned patterns from a completed "
        "plan. AUTO-LEARNING: marking the LAST phase complete automatically runs the "
        "retrospective (patterns land in the dedicated user-patterns store — the agent "
        "grows with the user).",
        "handler": _plan_update,
        "params": {
            "workspace_path": {"type": "string", "required": True, "desc": "Absolute path to project root"},
            "project_id": {"type": "string", "desc": "Optional project ID"},
            "mode": {
                "type": "string",
                "enum": ["switch", "mark_phase", "resolve", "retrospective"],
                "desc": "Operation (inferred if omitted)",
            },
            "plan_uuid": {"type": "string", "pattern": r"^[a-z0-9]{8}$", "desc": "Target plan UUID"},
            "phase_number": {"type": "integer", "desc": "Phase number (mark_phase)"},
        },
        "returns": (
            "{success, ...} | {success, patterns_extracted, plan_summary, suggested_patterns} "
            "for retrospective; auto_learned set when last phase auto-learns"
        ),
        "example": (
            'action_call(action="plan_update", params={"mode": "mark_phase", '
            '"plan_uuid": "mcptool1", "phase_number": 2})'
        ),
        "preconditions": ["workspace_valid", "plan_uuid_valid"],
        "mutates": True,
        "aliases": ["reg_switch_active_plan", "reg_mark_phase_complete", "reg_resolve_deferred_tasks"],
    },
    "reg_update": {
        "group": "plan",
        "summary": "Single registry.md CRUD: create / update status / delete a plan row.",
        "doc": "type=create generates the UUID server-side (agent saves tokens) and adds an "
        "Active row (⏹️) with Date + Created At. type=update moves the row to the correct "
        "table by status (active|paused|complete), refreshes Date, never touches Created At, "
        "and optionally updates summary. type=delete REMOVES the row but requires "
        "confirmed=true (strict user approval — without it the server refuses).",
        "handler": _reg_update,
        "params": {
            "workspace_path": {"type": "string", "required": True, "desc": "Absolute path to project root"},
            "project_id": {"type": "string", "desc": "Optional project ID"},
            "type": {"type": "string", "enum": ["create", "update", "delete"], "desc": "Registry operation"},
            "summary": {"type": "string", "desc": "Summary for create, or new summary for update"},
            "uuid": {"type": "string", "pattern": r"^[a-z0-9]{8}$", "desc": "Target plan UUID (update/delete)"},
            "status": {"type": "string", "enum": ["active", "paused", "complete"], "desc": "Target status (update)"},
            "confirmed": {
                "type": "boolean",
                "default": False,
                "desc": "User approval for delete (required)",
            },
        },
        "returns": (
            "{success, created_uuid, table, date, created_at} | {updated_uuid, status, moved_from, moved_to, "
            "date, created_at} | {deleted_uuid, deleted_from} | {needs_approval} on unconfirmed delete"
        ),
        "example": 'action_call(action="reg_update", params={"type": "create", "summary": "New plan"})',
        "preconditions": ["workspace_valid"],
        "mutates": True,
    },
    # ── Workflow ──────────────────────────────────────────────────────────
    "wf": {
        "group": "workflow",
        "summary": "List or execute a workflow.",
        "doc": "action=list (default) lists available workflows; action=execute runs a named "
        "workflow with optional JSON params. Workflows are workspace-free step definitions "
        "loaded from the shared work-flows dir (~/.awlab-id/agent-memory/work-flows); "
        "workspace_path is optional. Pass workflows_dir to override the location.",
        "handler": _wf,
        "params": {
            "workspace_path": {"type": "string", "desc": "Optional project root (workflows are workspace-free)"},
            "action": {"type": "string", "enum": ["list", "execute"], "default": "list"},
            "workflow_name": {"type": "string", "desc": "Workflow filename without .md"},
            "params": {"type": "string", "desc": "Optional JSON string of workflow params"},
            "workflows_dir": {"type": "string", "desc": "Optional override for the workflows directory"},
        },
        "returns": "{success, workflows|result}",
        "example": 'action_call(action="wf", params={"action": "execute", "workflow_name": "scan-project"})',
        "aliases": ["wf_execute", "wf_list"],
    },
    # ── Plan documents ────────────────────────────────────────────────────
    "plan_doc": {
        "group": "plan",
        "summary": "Read / create / update / delete a plan's plan.md or notes.md directly.",
        "doc": "Pass the FULL content (no template, no IDE compare-changes); review the "
        "complete result, not a diff. doc=plan (default) | notes. mode=read (default) | "
        "write | delete. write upserts the whole file atomically; delete removes it.",
        "handler": _plan_doc,
        "params": {
            "workspace_path": {"type": "string", "required": True, "desc": "Absolute path to project root"},
            "plan_uuid": {
                "type": "string",
                "required": True,
                "pattern": r"^[a-z0-9]{8}$",
                "desc": "8-char lowercase UUID",
            },
            "project_id": {"type": "string", "desc": "Optional project ID for agent-recall isolation"},
            "doc": {"type": "string", "enum": ["plan", "notes"], "default": "plan"},
            "mode": {"type": "string", "enum": ["read", "write", "delete"], "default": "read"},
            "content": {"type": "string", "desc": "Full markdown content (required for write)"},
        },
        "returns": "{success, doc, mode, path} | {content} on read",
        "example": (
            'action_call(action="plan_doc", params={"plan_uuid": "ab12cd34", "doc": "plan", '
            '"mode": "write", "content": "# Plan\\n\\n## Overview\\n"})'
        ),
        "preconditions": ["workspace_valid", "plan_uuid_valid"],
        "mutates": True,
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
    # ── Project identity ──────────────────────────────────────────────────
    "project_id": {
        "group": "context",
        "summary": "Check the project-id; auto-create it if missing (idempotent).",
        "doc": "Reads .ai/project-id. If missing (or force_regenerate), derives the "
        "sanitized directory-name slug (rule 08) and writes it. The agent MUST call this "
        "on FIRST response, BEFORE any mem_*/plan op, so memory isolation never falls "
        "through to the user-wide global DB. One call replaces the old long check+create "
        "flow — check and create are unified.",
        "handler": _project_id_check,
        "params": {
            "workspace_path": {"type": "string", "required": True, "desc": "Absolute path to project root"},
            "project_id": {"type": "string", "desc": "Reserved (informational)"},
            "force_regenerate": {"type": "boolean", "default": False, "desc": "Regenerate even if one exists"},
        },
        "returns": "{success, project_id, action: check|create, created, path}",
        "example": 'action_call(action="project_id", params={"workspace_path": "..."})',
        "preconditions": ["workspace_valid"],
        "mutates": True,
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
    "mem_observe": {
        "group": "memory",
        "summary": "Record user-pattern evidence into the observation store (baking input).",
        "doc": "Agent-relayed observations: chat/behavior signals (explicit statements, "
        "corrections, repeated commands) that the agent noticed. Each observation is a "
        "raw signal {signature, value, source, stack?, context?} appended to "
        ".ai/memory-bank/observations.jsonl (dedup/delta-guarded by fingerprint, so "
        "re-recording the same signal is a no-op). The baking pipeline later keys → "
        "counts → measures consistency → computes confidence.",
        "handler": _mem_observe,
        "params": {
            "workspace_path": {"type": "string", "required": True, "desc": "Absolute path to project root"},
            "project_id": {"type": "string", "desc": "Optional project scope (tag, not required)"},
            "observations": {
                "type": "array",
                "items": {"type": "object"},
                "desc": "[{signature, value, source?, stack?, context?}] — raw pattern evidence",
            },
            "stack": {"type": "string", "default": "any", "desc": "Default stack tag when an observation omits stack"},
        },
        "returns": "{success, store, appended, skipped_duplicates, skipped_invalid}",
        "example": (
            'action_call(action="mem_observe", params={"observations": [{"signature": '
            '"cmd_pnpm_install", "value": "pnpm install", "source": "behavioral", "stack": "nodejs"}]})'
        ),
        "preconditions": ["workspace_valid"],
        "mutates": True,
    },
    "mem_search": {
        "group": "memory",
        "summary": "Hybrid BM25+dense search over memory (optionally by entity type).",
        "doc": "Search the knowledge graph with hybrid BM25+dense ranking over name + "
        "observations. Set entity_type to filter by the exact entityType field — "
        "this is how you list patterns (entity_type='pattern'). With entity_type set "
        "and query empty, lists ALL entities of that type deterministically. "
        "store='patterns' searches the dedicated cross-project user-patterns store, "
        "scoped by `scope` (stack|project|all) with optional `context` area filter — "
        "results carry full provenance (stack/context/source_project/confidence).",
        "handler": memory_tools.search_memory,
        "params": {
            "workspace_path": {"type": "string", "required": True, "desc": "Absolute path to project root"},
            "query": {"type": "string", "default": "", "desc": "Search query (empty allowed with entity_type)"},
            "project_id": {"type": "string", "desc": "Optional project scope"},
            "limit": {"type": "integer", "default": 10, "desc": "Max results"},
            "use_dense": {"type": "boolean", "default": False, "desc": "Enable BM25+dense re-rank"},
            "entity_type": {"type": "string", "desc": "Exact entityType filter (e.g. 'pattern')"},
            "scope": {
                "type": "string",
                "enum": ["stack", "project", "all"],
                "default": "stack",
                "desc": "Pattern retrieval scope (store='patterns'): stack (auto-detected) | project | all",
            },
            "context": {"type": "string", "desc": "Optional pattern area filter (matches context/value)"},
            "store": {
                "type": "string",
                "pattern": r"^(project|patterns|family_[a-z0-9_-]+)$",
                "default": "project",
                "desc": "project memory (default), 'patterns' (user-patterns store), or family_<slug>",
            },
        },
        "returns": "{success, data:[...], filtered_by?, store, scope}",
        "example": 'action_call(action="mem_search", params={"query": "registry schema"})',
        "preconditions": ["workspace_valid"],
        "aliases": ["mem_list_patterns"],
    },
    "mem_write": {
        "group": "memory",
        "summary": "Create/tag entities, add observations, or relate entities.",
        "doc": "Pass entities, observations, and/or relations in one call. Observations "
        "auto-create their entity if missing. store='patterns' writes to the dedicated "
        "cross-project user-patterns store.",
        "handler": _mem_write,
        "params": {
            "workspace_path": {"type": "string", "required": True, "desc": "Absolute path to project root"},
            "project_id": {"type": "string", "desc": "Optional project scope"},
            "entities": {"type": "array", "items": {"type": "object"}, "desc": "[{name, entityType, observations}]"},
            "observations": {"type": "array", "items": {"type": "object"}, "desc": "[{entityName, contents}]"},
            "relations": {"type": "array", "items": {"type": "object"}, "desc": "[{from, to, relationType}]"},
            "store": {
                "type": "string",
                "pattern": r"^(project|patterns|family_[a-z0-9_-]+)$",
                "default": "project",
                "desc": "project memory (default), 'patterns' (user-patterns store), or family_<slug>",
            },
        },
        "returns": "{success, store, created, observations, relations}",
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
            "store": {
                "type": "string",
                "pattern": r"^(project|patterns|family_[a-z0-9_-]+)$",
                "default": "project",
                "desc": "project memory (default), 'patterns' (user-patterns store), or family_<slug>",
            },
        },
        "returns": "{success, node|graph}",
        "example": 'action_call(action="mem_read", params={"node": "MCPBridge"})',
        "preconditions": ["workspace_valid"],
        "aliases": ["mem_fetch_node_details", "mem_read_graph"],
    },
    "mem_remove": {
        "group": "memory",
        "summary": "Archive entities or delete observations/relations (type-safe).",
        "doc": "names → archive entities by name (REFUSED with a candidate list when the name "
        "matches multiple entityTypes — never guess); entities → precise [{name, entityType}] "
        "specs that archive exactly one; deletions → remove observations; relations → remove "
        "relations. Pass any combination in one call. store='patterns' routes to the "
        "dedicated user-patterns store.",
        "handler": _mem_remove,
        "params": {
            "workspace_path": {"type": "string", "required": True, "desc": "Absolute path to project root"},
            "project_id": {"type": "string", "desc": "Optional project scope"},
            "names": {
                "type": "array",
                "items": {"type": "string"},
                "desc": "Entities to archive by name (must be unambiguous)",
            },
            "entities": {
                "type": "array",
                "items": {"type": "object"},
                "desc": "[{name, entityType}] — archive the exact entity (safe when a name is ambiguous)",
            },
            "deletions": {"type": "array", "items": {"type": "object"}, "desc": "[{entityName, observations}]"},
            "relations": {"type": "array", "items": {"type": "object"}, "desc": "[{from, to, relationType}]"},
            "store": {
                "type": "string",
                "pattern": r"^(project|patterns|family_[a-z0-9_-]+)$",
                "default": "project",
                "desc": "project memory (default), 'patterns' (user-patterns store), or family_<slug>",
            },
        },
        "returns": "{success, store, archived, deleted_observations, deleted_relations}",
        "example": ('action_call(action="mem_remove", params={"entities": [{"name": "X", "entityType": "concept"}]})'),
        "preconditions": ["workspace_valid"],
        "mutates": True,
        "aliases": ["mem_archive_entities", "mem_delete_observations", "mem_delete_relations"],
    },
    "mem_list_entities": {
        "group": "memory",
        "summary": "List all memory entities (name/type/obs count) for auditing.",
        "doc": "Deterministic inventory of the whole store: total entities/relations, counts by "
        "entityType, and every entity as {name, entityType, observation_count}. Use to audit "
        "duplicates/staleness or discover what is stored before querying. store='patterns' "
        "inventories the dedicated user-patterns store.",
        "handler": _memory_inventory,
        "params": {
            "workspace_path": {"type": "string", "required": True, "desc": "Absolute path to project root"},
            "project_id": {"type": "string", "desc": "Optional project scope"},
            "limit": {"type": "integer", "default": 100, "desc": "Max entities returned"},
            "store": {
                "type": "string",
                "pattern": r"^(project|patterns|family_[a-z0-9_-]+)$",
                "default": "project",
                "desc": "project memory (default), 'patterns' (user-patterns store), or family_<slug>",
            },
        },
        "returns": (
            "{success, mode='inventory', store, total_entities, total_relations, by_type, "
            "entities:[{name, entityType, observation_count}]}"
        ),
        "example": 'action_call(action="mem_list_entities", params={"limit": 200})',
        "preconditions": ["workspace_valid"],
    },
    "mem_dedupe": {
        "group": "memory",
        "summary": "Merge same-named memory entities (keep data-bearing, archive dupes).",
        "doc": "For every name with multiple entities (optionally only `name`): picks the keeper "
        "(most observations), moves the others' observations into it, and archives the rest. "
        "dry_run=true (default) returns the plan without mutating — run it first, then "
        "dry_run=false to apply. store='patterns' dedupes the user-patterns store.",
        "handler": _mem_dedupe,
        "params": {
            "workspace_path": {"type": "string", "required": True, "desc": "Absolute path to project root"},
            "project_id": {"type": "string", "desc": "Optional project scope"},
            "name": {"type": "string", "desc": "Only merge this exact name (default: all names)"},
            "dry_run": {"type": "boolean", "default": True, "desc": "Preview without mutating"},
            "store": {
                "type": "string",
                "pattern": r"^(project|patterns|family_[a-z0-9_-]+)$",
                "default": "project",
                "desc": "project memory (default), 'patterns' (user-patterns store), or family_<slug>",
            },
        },
        "returns": "{success, store, dry_run, groups:[{name, keeper, duplicates}], moved_observations, archived}",
        "example": 'action_call(action="mem_dedupe", params={"name": "Bus Service"})',
        "preconditions": ["workspace_valid"],
        "mutates": True,
    },
    "mem_replay": {
        "group": "memory",
        "summary": "Replay the offline cache (pending.jsonl): re-apply queued mutations.",
        "doc": "Drains the offline cache at .ai/memory-bank/pending.jsonl (one JSON "
        "object per line). Entries are queued when a store write fails (mem_write/mem_remove "
        "store down, task_update DB sync down) or by an agent directly when the MCP server is "
        "unreachable (rule 14-mcp-offline-cache). Successful entries are removed; failed ones "
        "are kept for a later retry. dry_run=true previews the queue without applying.",
        "handler": _mem_replay,
        "params": {
            "workspace_path": {"type": "string", "required": True, "desc": "Absolute path to project root"},
            "dry_run": {"type": "boolean", "default": False, "desc": "Preview queued entries without applying"},
        },
        "returns": "{success, processed, succeeded, failed, pending_left, pending_path}",
        "example": 'action_call(action="mem_replay", params={"dry_run": True})',
        "preconditions": ["workspace_valid"],
        "mutates": True,
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
            "family": {
                "type": "string",
                "desc": (
                    "Family slug — build the MERGED family graph "
                    "(per-member builds + member:: tag merge; works across drives)"
                ),
            },
            "include_html": {"type": "boolean", "default": True, "desc": "Also export graph.html"},
            "directed": {"type": "boolean", "default": False, "desc": "Directed graph"},
            "project_id": {"type": "string", "desc": "Optional project ID for feedback-memory scoping"},
        },
        "returns": (
            "{success, out_dir, nodes, edges, files, artifacts} — rebuild writes "
            "graphify_feedback memory obs when files changed; if a background rebuild "
            "is already in flight this coalesces and returns {rebuilding: true}"
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
            "family": {
                "type": "string",
                "desc": "Family slug — report on the merged family graph (member:: tagged nodes)",
            },
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
            "The graph is AST-only and indexes file/function/component labels; when no node "
            "matches, this falls back to a whole-word source scan and returns file-level "
            "identifier hits (mode='identifier') so variable queries never dead-end. "
            "The graph_fresh precondition rebuilds the graph first if source files changed. "
            "Result includes freshness metadata: graph_fresh / graph_exists / "
            "graph_rebuilding / graph_built_at — when graph_rebuilding is true the read "
            "may have served the previous graph; re-read after a moment."
        ),
        "handler": _graph_query,
        "params": {
            "workspace_path": {"type": "string", "required": True, "desc": "Absolute path to project root"},
            "query": {"type": "string", "required": True, "desc": "Search term"},
            "limit": {"type": "integer", "default": 10, "desc": "Max results"},
            "root": {"type": "string", "desc": "Scan root (defaults to workspace_path)"},
            "family": {"type": "string", "desc": "Family slug — query the merged family graph (member:: tagged nodes)"},
            "project_id": {"type": "string", "desc": "Optional project ID for related-memory scoping"},
        },
        "returns": (
            "{success, count, mode, results:[{id, label, type, source_file}], "
            "related_memory:[{name, observations}], graph_fresh, graph_exists, "
            "graph_rebuilding, graph_built_at}"
        ),
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
            "The graph_fresh precondition rebuilds the graph first if source files changed. "
            "Result includes freshness metadata: graph_fresh / graph_exists / "
            "graph_rebuilding / graph_built_at."
        ),
        "handler": _graph_path,
        "params": {
            "workspace_path": {"type": "string", "required": True, "desc": "Absolute path to project root"},
            "a": {"type": "string", "required": True, "desc": "From node label"},
            "b": {"type": "string", "required": True, "desc": "To node label"},
            "root": {"type": "string", "desc": "Scan root (defaults to workspace_path)"},
            "family": {"type": "string", "desc": "Family slug — path over the merged family graph"},
        },
        "returns": ("{success, path:[labels], hops, graph_fresh, graph_exists, graph_rebuilding, graph_built_at}"),
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
            "The graph_fresh precondition rebuilds the graph first if source files changed. "
            "Result includes freshness metadata: graph_fresh / graph_exists / "
            "graph_rebuilding / graph_built_at."
        ),
        "handler": _graph_explain,
        "params": {
            "workspace_path": {"type": "string", "required": True, "desc": "Absolute path to project root"},
            "node": {"type": "string", "required": True, "desc": "Node label"},
            "limit": {"type": "integer", "default": 30, "desc": "Max neighbours"},
            "root": {"type": "string", "desc": "Scan root (defaults to workspace_path)"},
            "family": {"type": "string", "desc": "Family slug — explain a node in the merged family graph"},
            "project_id": {"type": "string", "desc": "Optional project ID for related-memory scoping"},
        },
        "returns": (
            "{success, node:{id, label, type, source_file, source_location}, "
            "neighbours:[{node, relation}], related_memory:[{name, observations}], "
            "graph_fresh, graph_exists, graph_rebuilding, graph_built_at}"
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
    # background=True: a heavy stale rebuild runs in a background thread so the
    # graph read returns immediately (small incremental rebuilds stay sync).
    # family=<slug> keeps the merged family graph fresh (per-member builds + merge).
    result = _graph_ensure_fresh(workspace_path, background=True, family=params.get("family"))
    ok = bool(result.get("success"))
    if not ok:
        return ok, result.get("error", "graph_fresh failed"), {"graph_fresh": result}, False
    if result.get("background"):
        return True, "graph stale — rebuilding in background", {"graph_fresh": result}, False
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
        "name: awlab-ai-assistant",
        "description: Dispatch consolidated MCP actions via action_call(action=...).",
        "---",
        "",
        "# awlab-ai-assistant — Action Reference",
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
