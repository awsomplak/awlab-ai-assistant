"""
Fixtures and helpers for MCP server tests.

Creates temporary plan artifacts and registry files for testing.
"""

import asyncio
from pathlib import Path

import pytest

# Ensure mcp package is importable
import sys

xpath = str(Path(__file__).resolve().parent.parent)
print(f"Trying to import xpath: {xpath}")
sys.path.insert(0, xpath)

from mcp_server.config import settings

# ── Fixtures: Temporary Project Structure ──────────────────────────────────

@pytest.fixture
def temp_project_dir(tmp_path_factory: pytest.TempPathFactory):
    """Create a temporary directory mimicking a project with .ai/ artifacts."""
    tmp = tmp_path_factory.mktemp("agent-memory-test-conftest")
    tmpdir = Path(tmp)

    # tmpdir = tempfile.mkdtemp(prefix="mcp-test-")
    ai_dir = tmpdir / ".ai"
    artifacts_dir = ai_dir / "artifacts"
    memory_dir = ai_dir / "memory-bank"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    memory_dir.mkdir(parents=True, exist_ok=True)

    yield tmpdir


@pytest.fixture
def plan_uuid() -> str:
    """Return a valid 8-char plan UUID."""
    return "a1b2c3d4"


@pytest.fixture
def plan_dir(temp_project_dir: Path, plan_uuid: str) -> Path:
    """Create a plan directory with tasks.md and plan.md."""
    pdir = settings.get_plan_dir(
        workspace_path=temp_project_dir,
        plan_uuid=plan_uuid
    )
    pdir.mkdir(parents=True, exist_ok=True)
    return pdir


# ── Fixtures: Sample File Content ──────────────────────────────────────────


SAMPLE_TASKS_MD = """# Tasks

## Phase 1: Backend Auth
- [ ] Task 1: Implement JWT authentication
    - [ ] Task 1.1: Create JWT utility
    - [ ] Task 1.2: Add login endpoint
- [x] Task 2: Setup database models
    - [x] Task 2.1: Create User model
    - [x✓] Task 2.2: Run migrations

## Phase 2: Frontend Auth
- [ ] Task 3: Login page
    - [ ] Task 3.1: Build form UI
    - [ ] Task 3.2: Add validation
- [x!] Task 4: Token storage
    - [x!] Task 4.1: Implement secure storage
"""

SAMPLE_REGISTRY_MD = """# Active Registry Plan

| UUID | Status | Date | Summary |
|------|--------|------|---------|
| a1b2c3d4 | ⏹️ | 2026-06-06 10:30 | User authentication flow |

# Paused Registry Plan

| UUID | Status | Date | Summary |
|------|--------|------|---------|
| e5f6g7h8 | ⏸️ | 2026-06-05 15:00 | Database schema redesign |

# Completed Registry Plan

| UUID | Status | Date | Summary |
|------|--------|------|---------|
| i9j0k1l2 | ✅ | 2026-06-04 09:00 | Initial project setup |
"""

SAMPLE_ENV_MD = """# Environment

## Shell
- **Default Shell**: PowerShell
- **Operating System**: Windows 11
"""

SAMPLE_PLAN_MD = """# Plan: User Authentication Flow

## Goal
Implement JWT-based authentication with login, register, and token refresh.

## Phases
1. Backend Auth
2. Frontend Auth
3. Security Hardening
"""


@pytest.fixture
def setup_project_id(temp_project_dir: Path) -> str:
    """Write a project-id file."""
    path = settings.get_project_id_path(workspace_path=temp_project_dir)
    path.write_text("test-project", encoding="utf-8")
    return str(path)

@pytest.fixture
def project_id(setup_project_id: str) -> str:
    """Return a valid project-id"""
    path = Path(setup_project_id)
    return path.read_text(encoding="utf-8")

@pytest.fixture
def setup_env_md(temp_project_dir: Path) -> str:
    """Write sample environment.md."""
    path = settings.get_memory_bank_dir(workspace_path=temp_project_dir) / "environment.md"
    path.write_text(SAMPLE_ENV_MD, encoding="utf-8")
    return str(path)


@pytest.fixture
def setup_registry_md(temp_project_dir: Path) -> str:
    """Write sample registry.md."""
    path = settings.get_registry_path(workspace_path=temp_project_dir)
    path.write_text(SAMPLE_REGISTRY_MD, encoding="utf-8")
    return str(path)


FULLY_COMPLETE_TASKS_MD = """# Tasks

## Phase 1: Backend Auth
- [x] Task 1: Implement JWT authentication
    - [x] Task 1.1: Create JWT utility
    - [x] Task 1.2: Add login endpoint
- [x✓] Task 2: Setup database models
    - [x✓] Task 2.1: Create User model
    - [x✓] Task 2.2: Run migrations

## Phase 2: Frontend Auth
- [ ] Task 3: Login page
    - [ ] Task 3.1: Build form UI
    - [ ] Task 3.2: Add validation
- [x!] Task 4: Token storage
    - [x!] Task 4.1: Implement secure storage
"""


@pytest.fixture
def full_complete_tasks_md(plan_dir: Path, temp_project_dir: Path) -> str:
    """Write a tasks.md with all Phase 1 tasks already terminal, plus registry.md."""
    path = plan_dir / "tasks.md"
    path.write_text(FULLY_COMPLETE_TASKS_MD, encoding="utf-8")

    # Also write registry.md so update_registry_phase_count succeeds
    registry_path = temp_project_dir / ".ai" / "artifacts" / "registry.md"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(SAMPLE_REGISTRY_MD, encoding="utf-8")

    return str(path)


@pytest.fixture
def setup_tasks_md(plan_dir: Path) -> str:
    """Write sample tasks.md to the plan directory."""
    path = plan_dir / "tasks.md"
    path.write_text(SAMPLE_TASKS_MD, encoding="utf-8")
    return str(path)


@pytest.fixture
def setup_plan_md(plan_dir: Path) -> str:
    """Write sample plan.md to the plan directory."""
    path = plan_dir / "plan.md"
    path.write_text(SAMPLE_PLAN_MD, encoding="utf-8")
    return str(path)


# ── Mock fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def mock_agent_recall_success(monkeypatch):
    """
    Mock agent_recall module functions so that DB sync calls
    succeed silently without requiring an actual agent-recall database.
    """
    def _fake_create_entities(workspace_path, entities, project_id=None):
        return {"created": len(entities)}

    def _fake_add_observations(workspace_path: str | Path, observations, project_id=None):
        return {"added": len(observations), "blocked": 0}

    def _fake_search_nodes(workspace_path: str | Path, query, limit=10, project_id=None):
        return [
            {
                "name": "pattern_preference_pnpm",
                "entityType": "pattern",
                "observations": [
                    "type: preference",
                    "value: use pnpm for package management",
                    "confidence: 0.9",
                    "source: explicit"
                ]
            }
        ]

    import mcp_server.helpers.agent_recall as ar_mod
    monkeypatch.setattr(ar_mod, "create_entities", _fake_create_entities)
    monkeypatch.setattr(ar_mod, "add_observations", _fake_add_observations)
    monkeypatch.setattr(ar_mod, "search_nodes", _fake_search_nodes)
    monkeypatch.setattr(ar_mod, "create_relations", _fake_create_entities)
    yield


# ── Async adapter for non-async tools ──────────────────────────────────────


def run_async(coro):
    """Run an async coroutine synchronously in tests."""
    return asyncio.run(coro)
