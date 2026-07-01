"""MCP helpers: file utilities, registry parsing, agent-recall bridge, and logging."""

from .logger import Logger, logger
from .workspace import resolve_db_path
from .agent_recall import (
    create_bridge,
    search_nodes,
    open_nodes,
    read_graph,
    create_entities,
    add_observations,
    create_relations,
    delete_entities,
    delete_relations,
    delete_observations,
)
from .registry_utils import (
    load_registry,
    parse_registry,
    rebuild_registry_content,
    list_active_plans,
    list_paused_plans,
    list_completed_plans,
    switch_active_plan,
)
from .file_utils import (
    read_file_safe as read_utf8,
    write_file_safe as write_utf8,
    read_registry_md,
    read_plan_md,
    read_tasks_md,
    parse_tasks_md,
    update_task_status_in_md,
    get_task_status,
    get_tasks_in_phase,
    has_incomplete_tasks_in_phase,
    get_next_eligible_task,
    compute_tasks_summary,
    read_memory_bank_file,
)
from .validation import (
    validate_project_root,
    validate_workspace_path,
    validate_project_id,
    validate_uuid,
    validate_status,
    validate_status_transition,
    invalid_uuid,
    invalid_status,
    invalid_phase_number,
    require_uuid,
    require_status,
    require_phase_number,
    invalid_format,
    invalid_scope,
    invalid_status_marker,
    VALID_STATUS_MARKERS,
)
from .response import (
    resp_obj,
    resp_json,
    ok_obj,
    ok_json,
    fail_obj,
    fail_json,
    validate_resp,
)

__all__ = [
    # Logger
    "Logger",
    "logger",

    # Workspace
    "resolve_db_path",

    # Agent-recall
    "create_bridge",
    "search_nodes",
    "open_nodes",
    "read_graph",
    "create_entities",
    "add_observations",
    "create_relations",
    "delete_entities",
    "delete_relations",
    "delete_observations",

    # Registry
    "load_registry",
    "parse_registry",
    "rebuild_registry_content",
    "list_active_plans",
    "list_paused_plans",
    "list_completed_plans",
    "switch_active_plan",

    # File utils
    "write_utf8",
    "read_utf8",
    "read_registry_md",
    "read_plan_md",
    "read_tasks_md",
    "read_memory_bank_file",
    "parse_tasks_md",
    "get_task_status",
    "get_tasks_in_phase",
    "get_next_eligible_task",
    "update_task_status_in_md",
    "has_incomplete_tasks_in_phase",
    "compute_tasks_summary",

    # Response
    "resp_obj",
    "resp_json",
    "ok_obj",
    "ok_json",
    "fail_obj",
    "fail_json",

    # Validation
    "validate_project_root",
    "validate_workspace_path",
    "validate_project_id",
    "validate_uuid",
    "validate_status",
    "validate_status_transition",
    "validate_resp",

    # Require fallback
    "require_uuid",
    "require_status",
    "require_phase_number",

    # Invalid fallback
    "invalid_uuid",
    "invalid_status",
    "invalid_phase_number",
    "invalid_format",
    "invalid_scope",
    "invalid_status_marker",

    # Constants
    "VALID_STATUS_MARKERS",
]
