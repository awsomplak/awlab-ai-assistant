"""
Phase 2 — observation store + mem_observe.

Locks in:
1. Observation record shape + normalization.
2. observations.jsonl atomic append + torn-tail tolerant reader.
3. Dedup/delta guard (fingerprint) — re-recording is a no-op.
4. mem_observe REGISTRY handler.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_server.helpers.observation_store import (
    append_observations,
    build_record,
    clear_observations,
    normalize_record,
    observations_path,
    read_observations,
)
from mcp_server.registry import _mem_observe

# ── Record shape ─────────────────────────────────────────────────────────────


def test_normalize_record_defaults(tmp_path: Path):
    rec = normalize_record({"signature": "cmd_pnpm", "value": "pnpm install"})
    assert rec["signature"] == "cmd_pnpm"
    assert rec["value"] == "pnpm install"
    assert rec["source"] == "inferred"
    assert rec["stack"] == "any"
    assert rec["project"] == ""
    assert rec["fingerprint"]  # dedup guard present


def test_normalize_record_rejects_blank(tmp_path: Path):
    assert normalize_record({"signature": "", "value": "x"}) == {}
    assert normalize_record({"signature": "x", "value": "  "}) == {}


def test_build_record_mined_fingerprint_changes_with_source():
    a = build_record("style", "no-semicolons", source="mined", path="/f.ts", mtime=1.0, stat="abc")
    b = build_record("style", "no-semicolons", source="mined", path="/f.ts", mtime=2.0, stat="abc")
    assert a["fingerprint"] != b["fingerprint"]  # source change invalidates old fingerprint


# ── Append + torn-tail reader ────────────────────────────────────────────────


def test_append_and_read(tmp_path: Path):
    res = append_observations(tmp_path, [{"signature": "a", "value": "1"}, {"signature": "b", "value": "2"}])
    assert res["success"] and res["appended"] == 2
    obs = read_observations(tmp_path)
    assert len(obs) == 2
    assert {o["signature"] for o in obs} == {"a", "b"}


def test_append_torn_tail_tolerated(tmp_path: Path):
    append_observations(tmp_path, [{"signature": "a", "value": "1"}])
    p = observations_path(tmp_path)
    with open(p, "a", encoding="utf-8") as f:
        f.write('{"signature": "b", "val')  # torn line
    obs = read_observations(tmp_path)
    assert len(obs) == 1  # corrupt tail skipped
    assert obs[0]["signature"] == "a"


# ── Dedup/delta guard ────────────────────────────────────────────────────────


def test_dedup_skips_duplicate(tmp_path: Path):
    rec = {"signature": "cmd_pnpm", "value": "pnpm install", "source": "behavioral"}
    r1 = append_observations(tmp_path, [rec])
    r2 = append_observations(tmp_path, [rec])
    assert r1["appended"] == 1
    assert r2["appended"] == 0
    assert r2["skipped_duplicates"] == 1
    assert len(read_observations(tmp_path)) == 1


def test_same_value_new_signature_appends(tmp_path: Path):
    append_observations(tmp_path, [{"signature": "sig_a", "value": "pnpm install"}])
    r = append_observations(tmp_path, [{"signature": "sig_b", "value": "pnpm install"}])
    assert r["appended"] == 1  # different signature = different fingerprint


# ── mem_observe REGISTRY handler ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mem_observe_appends(tmp_path: Path):
    r = await _mem_observe(
        str(tmp_path),
        observations=[{"signature": "cmd_pnpm", "value": "pnpm install", "source": "behavioral", "stack": "nodejs"}],
    )
    assert r["success"]
    assert r["appended"] == 1
    assert read_observations(tmp_path)[0]["stack"] == "nodejs"
    clear_observations(tmp_path)


@pytest.mark.asyncio
async def test_mem_observe_dedup(tmp_path: Path):
    obs = [{"signature": "cmd_pnpm", "value": "pnpm install", "source": "behavioral"}]
    await _mem_observe(str(tmp_path), observations=obs)
    r2 = await _mem_observe(str(tmp_path), observations=obs)
    assert r2["appended"] == 0
    assert r2["skipped_duplicates"] == 1
    clear_observations(tmp_path)


@pytest.mark.asyncio
async def test_mem_observe_requires_observations(tmp_path: Path):
    r = await _mem_observe(str(tmp_path), observations=[])
    assert not r["success"]
