"""
plan_tools package — modular plan, task, phase, and registry management.

Re-exports all public functions from submodules so that existing imports
like ``from mcp_server.tools.plan_tools import read_plan_tasks`` continue
to work without changes.
"""

# ── IO (file disk operations, agent-recall sync) ───────────────────────────
from .io import (
    store_memory_checkpoint,
    store_pattern_entity,
    sync_to_agent_recall,
    update_registry_phase_count,
)

# ── Phase (gate validation) ─────────────────────────────────────────────────
from .phase import (
    validate_phase_gate,
)

# ── Plan (registry, workflows, retrospective) ───────────────────────────────
from .plan import (
    check_plan_completable,
    create_registry_entry,
    delete_registry_entry,
    execute_workflow,
    generate_retrospective_summary,
    get_next_eligible_task,
    list_registry,
    list_workflows,
    mark_phase_complete,
    resolve_deferred_tasks,
    switch_active_plan,
    update_registry_status,
)

# ── Tasks (CRUD for tasks.md) ───────────────────────────────────────────────
from .tasks import (
    batch_update_tasks,
    read_plan_tasks,
    update_task_status,
    write_plan_tasks,
)

__all__ = [
    # io
    "sync_to_agent_recall",
    "store_memory_checkpoint",
    "update_registry_phase_count",
    "store_pattern_entity",
    # tasks
    "read_plan_tasks",
    "update_task_status",
    "batch_update_tasks",
    "write_plan_tasks",
    # phase
    "validate_phase_gate",
    # plan (also includes get_next_eligible_task)
    "get_next_eligible_task",
    # plan
    "list_registry",
    "switch_active_plan",
    "mark_phase_complete",
    "resolve_deferred_tasks",
    "check_plan_completable",
    "execute_workflow",
    "list_workflows",
    "generate_retrospective_summary",
    # registry CRUD (reg_update)
    "create_registry_entry",
    "update_registry_status",
    "delete_registry_entry",
]
