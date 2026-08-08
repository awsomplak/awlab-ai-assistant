"""Vulture whitelist — names used dynamically/externally (false positives).

The repeatable dead-code check (``python scripts/lint.py --deadcode``) scans
this file alongside ``src``; any symbol referenced here is treated as used, so
legitimately dynamic/external names are whitelisted instead of being reported.

Only add symbols that are genuinely used from outside ``src`` (external tooling,
dynamic attributes) — never real dead code.

NOTE: ruff is configured to exclude this file (see ``pyproject.toml``), so the
bare references below are safe.
"""


def _whitelist(name):  # noqa: ANN001
    return name


# Manually invoked generator — regenerates assets/skills/awlab-mcp/SKILL.md
# from the REGISTRY; called from external tooling, never from within src/.
_whitelist(
    build_skill_md,
)

# Public family helpers — exported via helpers/__init__ for tests and agent
# tooling (auto-detecting a workspace's family, enumerating family slugs);
# referenced from outside src/, so vulture's src-only scan can't see the use.
from mcp_server.helpers import agent_recall

_whitelist(
    agent_recall.family_slugs,
    agent_recall.family_for_workspace,
)

# Public offline-cache API (rule 14-mcp-offline-cache) — used by the agent's own
# file tools and by tests (tests/test_pending_queue.py) to clear the queue after
# a successful manual replay; vulture's src-only scan can't see those uses.
from mcp_server.tools.plan_tools import io as plan_io

_whitelist(
    plan_io.clear_pending,
)
