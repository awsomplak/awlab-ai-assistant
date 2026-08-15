"""Live probe of the built exe over real stdio MCP.

Spawns ``dist/bin/awlab-ai-assistant.exe``, connects as an MCP client, and smoke-tests the
action surface a freshly built server must expose:

  - ``action_help`` → 23-action overview (plan_doc / project_id / mem_observe present)
  - ``util_info`` → version + build tag
  - ``project_id`` → check-and-create on a scratch workspace (idempotent)
  - ``mem_observe`` x4 → ``ctx_info mode="context"`` → pattern_candidates (baking + delivery)
  - ``ctx_info`` again → tell-once (empty)
  - ``mem_search store="patterns"`` → baked_patterns
  - plan lifecycle: ``reg_update`` create → ``task_update``/``task_read``/``plan_status`` →
    ``plan_doc`` write/read/delete → ``reg_update`` complete
  - memory graph ops: ``mem_write`` → ``mem_list_entities``/``mem_read``/``mem_search`` →
    ``mem_dedupe`` (dry-run) → ``mem_remove``
  - code graph: ``graph_build`` → ``graph_status`` → ``graph_query`` (scratch .py project)
  - offline cache: ``mem_replay`` (dry-run on empty → graceful)
  - hook mode: ``exe hook --agent copilot --event user-prompt-submit`` → exit 0 + JSON stdout

Usage (after ``python scripts/run.py build``):

    python scripts/live_probe.py

Exit code 0 = all green. The probe writes only under ``.ai/temp/live-probe/`` (scratch).
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Windows console is cp1252 — force UTF-8 so fancy chars in prints don't crash.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "dist" / "bin" / "awlab-ai-assistant.exe"
WS = ROOT / ".ai" / "temp" / "live-probe"

OBSERVATIONS = [
    {"signature": "cmd_pnpm", "value": "always use pnpm install", "source": "explicit", "stack": "any"},
    {"signature": "cmd_pnpm", "value": "Always use pnpm install.", "source": "explicit", "stack": "any"},
    {"signature": "cmd_pnpm", "value": "always  use pnpm install!", "source": "explicit", "stack": "any"},
    {"signature": "cmd_pnpm", "value": "ALWAYS USE PNPM INSTALL", "source": "explicit", "stack": "any"},
]

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))


async def main() -> int:
    if not EXE.exists():
        print(f"ERROR: {EXE} not found — build first (python scripts/run.py build)")
        return 1
    # Fresh scratch every run — dedup/tell-once/delivery state must not leak across runs.
    if WS.exists():
        shutil.rmtree(WS)
    (WS / ".ai").mkdir(parents=True, exist_ok=True)

    params = StdioServerParameters(command=str(EXE), args=[], env=None)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            async def call(action: str, p: dict | None = None) -> dict:
                res = await session.call_tool("action_call", {"action": action, "params": p or {}})
                text = res.content[0].text if res.content else "{}"
                return json.loads(text)

            print("\n== util_info (version/build) ==")
            info = await call("util_info")
            env = info.get("result", {}).get("environment", {})
            version = info.get("result", {}).get("version", info.get("result", {}))
            check("util_info returns result", info.get("success") is True, json.dumps(env)[:120])
            print("   version:", version)

            print("\n== action_help (action surface) ==")
            # action_help is an MCP *tool*, not a REGISTRY action — an unknown-action
            # error from action_call still lists valid_actions (the full 23).
            help_res = await call("action_help")
            raw = json.dumps(help_res)
            check("action surface lists plan_doc", "plan_doc" in raw)
            check("action surface lists project_id", "project_id" in raw)
            check("action surface lists mem_observe", "mem_observe" in raw)
            check("action surface lists mem_replay", "mem_replay" in raw)

            print(f"\n== project_id (check-and-create) on {WS} ==")
            r = await call("project_id", {"workspace_path": str(WS)})
            check("project_id ok", r.get("success") is True, json.dumps(r.get("result", r))[:140])
            check("project-id file written", (WS / ".ai" / "project-id").exists())

            print("\n== mem_observe x4 -> bake -> ctx_info delivery ==")
            for i, obs in enumerate(OBSERVATIONS, 1):
                r = await call("mem_observe", {"workspace_path": str(WS), "observations": [obs]})
                check(f"mem_observe #{i} appended", (r.get("result") or {}).get("appended") == 1)
            r = await call("ctx_info", {"workspace_path": str(WS), "mode": "context", "query": "pnpm"})
            cands = (r.get("result") or {}).get("pattern_candidates") or []
            check(
                "ctx_info → pattern_candidates baked",
                len(cands) == 1,
                f"sig={cands[0].get('signature') if cands else None} "
                f"conf={cands[0].get('confidence') if cands else None}",
            )
            r2 = await call("ctx_info", {"workspace_path": str(WS), "mode": "context", "query": "pnpm"})
            check("tell-once: 2nd ctx_info empty", ((r2.get("result") or {}).get("pattern_candidates") or []) == [])

            print("\n== mem_search store=patterns (baked_patterns) ==")
            r3 = await call("mem_search", {"workspace_path": str(WS), "store": "patterns", "query": "pnpm"})
            baked = (r3.get("result") or {}).get("baked_patterns") or []
            check("mem_search → baked_patterns", len(baked) == 1, f"sig={baked[0].get('signature') if baked else None}")

            print("\n== plan lifecycle (reg_update → task_update → plan_status → plan_doc) ==")

            def _uuid_of(r: dict) -> str:
                for scope in (r.get("result") or {}, r):
                    u = scope.get("uuid") or scope.get("created_uuid")
                    if u:
                        return str(u)
                return ""

            created = await call(
                "reg_update", {"workspace_path": str(WS), "type": "create", "summary": "live-probe plan"}
            )
            plan_uuid = _uuid_of(created)
            check("reg_update create → uuid", bool(plan_uuid), plan_uuid)

            tasks_md = "# Tasks\n\n## Phase 1: Probe\n\n- [ ] Task 1: live task\n"
            tu = await call("task_update", {"workspace_path": str(WS), "plan_uuid": plan_uuid, "content": tasks_md})
            check("task_update write tasks.md", tu.get("success") is True)
            tr = await call("task_read", {"workspace_path": str(WS), "plan_uuid": plan_uuid, "format": "structured"})
            check("task_read structured → 1 phase", len(((tr.get("result") or {}).get("phases")) or []) == 1)
            ps = await call("plan_status", {"workspace_path": str(WS), "plan_uuid": plan_uuid})
            check("plan_status ok", (ps.get("result") or {}).get("completable", {}).get("success") is True)

            pdw = await call(
                "plan_doc",
                {
                    "workspace_path": str(WS),
                    "plan_uuid": plan_uuid,
                    "doc": "notes",
                    "mode": "write",
                    "content": "# Notes\n\nprobe notes\n",
                },
            )
            check("plan_doc write notes.md", pdw.get("success") is True)
            pdr = await call(
                "plan_doc", {"workspace_path": str(WS), "plan_uuid": plan_uuid, "doc": "notes", "mode": "read"}
            )
            check("plan_doc read notes.md", (pdr.get("result") or {}).get("content") == "# Notes\n\nprobe notes\n")
            pdd = await call(
                "plan_doc", {"workspace_path": str(WS), "plan_uuid": plan_uuid, "doc": "notes", "mode": "delete"}
            )
            check("plan_doc delete notes.md", pdd.get("success") is True)

            ru = await call(
                "reg_update", {"workspace_path": str(WS), "type": "update", "uuid": plan_uuid, "status": "complete"}
            )
            check("reg_update → complete", ru.get("success") is True, json.dumps(ru.get("result", ru))[:100])

            print("\n== memory graph ops (mem_write → list → read → search → dedupe → remove) ==")
            mw = await call(
                "mem_write",
                {
                    "workspace_path": str(WS),
                    "entities": [{"name": "ProbeEntity", "entityType": "concept", "observations": []}],
                    "observations": [{"entityName": "ProbeEntity", "contents": ["live probe entity"]}],
                },
            )
            check("mem_write ok", mw.get("success") is True)
            ml = await call("mem_list_entities", {"workspace_path": str(WS)})
            names = [e.get("name") for e in ((ml.get("result") or {}).get("entities") or [])]
            check("mem_list_entities contains ProbeEntity", "ProbeEntity" in names)
            mr = await call("mem_read", {"workspace_path": str(WS), "node": "ProbeEntity"})
            check("mem_read node found", (mr.get("result") or {}).get("nodes") is not None)
            ms = await call("mem_search", {"workspace_path": str(WS), "query": "probe"})
            found = any(e.get("name") == "ProbeEntity" for e in ((ms.get("result") or {}).get("data") or []))
            check("mem_search finds ProbeEntity", found)
            md = await call("mem_dedupe", {"workspace_path": str(WS), "name": "ProbeEntity", "dry_run": True})
            check("mem_dedupe dry-run ok", md.get("success") is True)
            rm = await call("mem_remove", {"workspace_path": str(WS), "names": ["ProbeEntity"]})
            check("mem_remove archived", rm.get("success") is True)

            print("\n== code graph (graph_build → status → query) ==")
            app_dir = WS / "app"
            app_dir.mkdir(parents=True, exist_ok=True)
            (app_dir / "loader.py").write_text("def load_config():\n    return {}\n", encoding="utf-8")
            gb = await call("graph_build", {"workspace_path": str(WS)})
            check("graph_build ok", gb.get("success") is True)
            gs = await call("graph_status", {"workspace_path": str(WS)})
            check("graph_status exists", ((gs.get("result") or {}).get("exists")) is True)
            gq = await call("graph_query", {"workspace_path": str(WS), "query": "load_config"})
            hits = (gq.get("result") or {}).get("results") or []
            check(
                "graph_query finds load_config",
                any(str(h.get("label") or "").lower().startswith("load_config") for h in hits),
                json.dumps(
                    {
                        "mode": (gq.get("result") or {}).get("mode"),
                        "count": (gq.get("result") or {}).get("count"),
                        "hits": hits[:3],
                    }
                )[:220],
            )

            print("\n== offline cache (mem_replay dry-run on empty) ==")
            replay = await call("mem_replay", {"workspace_path": str(WS), "dry_run": True})
            check("mem_replay dry-run graceful", replay.get("success") is True)

    print("\n== hook mode (exe hook --agent claude --event PostToolUse — capture) ==")
    # A command-carrying TOOL event must append an observation (kind=tool → CAPTURE).
    hook_payload = json.dumps({"cwd": str(WS), "tool_name": "Bash", "tool_input": {"command": "pnpm install"}})
    hook = subprocess.run(
        [str(EXE), "hook", "--agent", "claude", "--event", "PostToolUse", "--project", str(WS)],
        input=hook_payload.encode("utf-8"),
        capture_output=True,
        timeout=120,
    )
    try:
        json.loads(hook.stdout.decode("utf-8", errors="replace") or "{}")
        stdout_json = True
    except Exception:  # noqa: BLE001
        stdout_json = False
    hook_obs = WS / ".ai" / "memory-bank" / "observations.jsonl"
    obs_text = hook_obs.read_text("utf-8") if hook_obs.exists() else ""
    check("hook mode exit 0", hook.returncode == 0, f"rc={hook.returncode}")
    check("hook stdout is JSON", stdout_json)
    check("hook capture writes observation", "hook_" in obs_text, f"lines={len(obs_text.splitlines())}")

    print(f"\n=== SUMMARY: {len(PASS)} passed, {len(FAIL)} failed ===")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
