"""
Regression tests: special characters must survive write/parse round-trips.

Covers:
- registry summaries (literal ``|``, unicode, emoji, quotes, brackets)
- task descriptions (unicode, ``[x]``, pipes, quotes, embedded newlines)
- the observation store (JSONL values)
"""

from pathlib import Path

from mcp_server.helpers.file_utils import (
    create_task_in_md,
    get_task_status,
    parse_tasks_md,
    update_task_status_in_md,
)
from mcp_server.helpers.observation_store import append_observations, read_observations
from mcp_server.helpers.registry_utils import (
    create_registry_entry,
    parse_registry,
    update_registry_status,
)

SPECIAL_SUMMARY = "Fix auth bug | also add ✅ emoji 🚀 'quotes' [x]"
SPECIAL_TASK = "Implement 🚀 漢字 auth — set 'X-Frame-Options' | with [x] marker & `code` #hash ✓"


class TestRegistrySpecialCharacters:
    def test_summary_with_pipe_roundtrips(self, temp_project_dir: Path):
        """A summary containing `|` must survive create → parse → update."""
        created = create_registry_entry(workspace_path=temp_project_dir, summary=SPECIAL_SUMMARY)
        assert created.get("success") is True

        parsed = parse_registry(temp_project_dir)
        assert parsed["success"] is True
        assert len(parsed["active"]) == 1
        assert parsed["active"][0]["summary"] == SPECIAL_SUMMARY

        # Status update must preserve the piped summary too.
        uuid_ = parsed["active"][0]["uuid"]
        updated = update_registry_status(workspace_path=temp_project_dir, uuid=uuid_, status="complete")
        assert updated.get("success") is True

        parsed2 = parse_registry(temp_project_dir)
        assert len(parsed2["completed"]) == 1
        assert parsed2["completed"][0]["summary"] == SPECIAL_SUMMARY

    def test_legacy_raw_pipe_summary_roundtrips(self, temp_project_dir: Path):
        """A legacy row whose summary contains a raw (unescaped) `|` is preserved."""
        from mcp_server.helpers.registry_utils import rebuild_registry_content

        entry = {
            "uuid": "ab12cd34",
            "status": "⏹️",
            "date": "2026-08-13",
            "created_at": "2026-08-13",
            "summary": "raw | pipe summary",
        }
        content = rebuild_registry_content([entry], [], [])
        registry_path = Path(temp_project_dir) / ".ai" / "artifacts" / "registry.md"
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        registry_path.write_text(content, encoding="utf-8")

        parsed = parse_registry(temp_project_dir)
        assert parsed["active"][0]["summary"] == "raw | pipe summary"


class TestTaskSpecialCharacters:
    def test_description_roundtrips(self):
        """Special characters in a task description survive create/parse/update."""
        content = "## Phase 1: Backend\n\n- [ ] 1.1: existing task\n"
        updated, path = create_task_in_md(content, "1.2", SPECIAL_TASK)
        assert updated is not None and path == "1.2"

        parsed = parse_tasks_md(updated)
        tasks = parsed["phases"][0]["tasks"]
        found = [t for t in tasks if t["path"] == "1.2"]
        assert found and found[0]["description"] == SPECIAL_TASK

        updated2 = update_task_status_in_md(updated, "1.2", "[x]")
        assert updated2 is not None
        assert SPECIAL_TASK in updated2
        assert get_task_status(updated2, "1.2") == "[x]"

    def test_description_with_newline_is_single_line(self):
        """Embedded newlines in a description are collapsed to a single line."""
        content = "## Phase 1: Backend\n\n- [ ] 1.1: existing task\n"
        multi_line = "Line one\nLine two\r\nLine three"
        updated, _ = create_task_in_md(content, "1.2", multi_line)
        assert updated is not None
        leaf = [ln for ln in updated.splitlines() if "Line one" in ln]
        assert len(leaf) == 1
        assert "Line one Line two Line three" in leaf[0]
        # The leaf must be a single markdown task line (no embedded newline).
        assert "\n" not in leaf[0].split("- [ ] ", 1)[-1]


class TestObservationSpecialCharacters:
    def test_observation_values_roundtrip(self, temp_project_dir: Path):
        """Special characters in observation values survive the JSONL store."""
        records = [
            {
                "signature": "style_pydantic_base_model",
                "value": "always use BaseModel with 🚀 漢字 — field='x' | pipe [ok]",
                "source": "behavioral",
                "stack": "python",
            }
        ]
        res = append_observations(workspace_path=temp_project_dir, records=records)
        assert res.get("success") is True
        assert res.get("appended") == 1

        obs = read_observations(temp_project_dir)
        assert len(obs) == 1
        assert obs[0]["signature"] == records[0]["signature"]
        assert obs[0]["value"] == records[0]["value"]
