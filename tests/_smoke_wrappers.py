import asyncio, json
from mcp_server.modules import registration
from mcp_server.modules import _wrappers

def show(label, payload):
    p = json.loads(payload) if isinstance(payload, str) else payload
    print(f"{label}: success={p.get('success')}, error={str(p.get('error', p.get('reason', '')))[:100]}")

async def main():
    await registration.update_task_status("aa", "1.1", "[ ]", ".")
    await registration.batch_update_tasks("aa", [], ".")
    await registration.validate_phase_gate("aa", 0, ".")
    await registration.validate_status_transition("[ ]", "bad")
    await registration.read_plan_tasks("aa", ".")
    await registration.search_memory_cross("query", "bad_scope")
    await registration.store_context("key", "value", "bad_scope")

    show("invalid_uuid", _wrappers.invalid_uuid())
    show("invalid_status", _wrappers.invalid_status("[bad]"))
    show("invalid_phase_number", _wrappers.invalid_phase_number(0))
    show("require_phase_number(0)", _wrappers.require_phase_number(0))
    show("require_phase_number(1)", _wrappers.require_phase_number(1) or _wrappers.ok(data={"x": 1}))
    show("invalid_format", _wrappers.invalid_format("format", "rar", ("structured", "raw")))
    show("invalid_scope", _wrappers.invalid_scope("mars"))
    show("invalid_status_marker", _wrappers.invalid_status_marker("[bad]", "current"))
    show("fail exception", _wrappers.fail("something broke"))
    show("ok", _wrappers.ok(data={"x": 1}))

asyncio.run(main())
