"""Tests for the baking pipeline (helpers/baking.py) — Phase 4."""

from pathlib import Path

from mcp_server.helpers.baking import (
    bake_tick,
    baked_path,
    cluster_key,
    compute_stats,
    deliver_candidates,
    delivery_marker,
    emerging_candidates,
    key_style,
    key_template,
    make_signature,
    mark_delivered,
    normalize_signature,
    read_baked,
    scope_candidates,
    undelivered_candidates,
)
from mcp_server.helpers.observation_store import append_observations


class TestSignatureKeying:
    def test_case_whitespace_insensitive(self):
        assert make_signature("  Always use BaseModel. ") == make_signature("always use BaseModel")
        assert make_signature("pnpm  install --save") == make_signature("pnpm install --save")

    def test_template_vs_style(self):
        assert key_template("always use BaseModel!") == "always use basemodel"
        assert key_style("use pnpm @ 2026") == "use pnpm 2026"

    def test_meaningless_value_falls_back_to_empty(self):
        assert make_signature("!!!") == ""

    def test_normalize_signature(self):
        assert normalize_signature("  cmd_pnpm_install. ") == "cmd_pnpm_install"
        assert normalize_signature("style_base_model") == "style_base_model"

    def test_cluster_key_prefers_signature_then_value(self):
        assert cluster_key({"signature": "cmd_pnpm", "value": "whatever"}) == "cmd_pnpm"
        assert cluster_key({"signature": "", "value": "always use pnpm"}) == "always use pnpm"


def _obs(signature: str, values: list[str], sources: list[str] | None = None) -> list[dict]:
    return [
        {
            "signature": signature,
            "value": v,
            "source": (sources or ["behavioral"] * len(values))[i],
            "stack": "python",
            "project": "proj",
            "ts": "2026-08-13T00:00:00+00:00",
        }
        for i, v in enumerate(values)
    ]


class TestComputeStats:
    def test_count_and_consistency_agreeing_values(self):
        # 3 distinct values that all normalize to one value key, same signature.
        obs = _obs("style_pnpm", ["always use pnpm", "Always use pnpm.", "always  use pnpm!"])
        stats = compute_stats(obs)
        assert len(stats) == 1
        s = stats[0]
        assert s["signature"] == "style_pnpm"
        assert s["count"] == 3
        assert s["consistency"] == 1.0
        # behavioral (0.6) × frequency (min(1, 3/5)=0.6) × consistency 1.0
        assert s["confidence"] == round(0.6 * 0.6 * 1.0, 3)

    def test_consistency_below_one_with_divergent_values(self):
        # 2 of 3 values agree → consistency 2/3.
        obs = _obs("cmd_pnpm", ["pnpm install", "pnpm install", "npm install"])
        stats = compute_stats(obs)
        assert stats[0]["signature"] == "cmd_pnpm"
        assert stats[0]["consistency"] == round(2 / 3, 3)

    def test_source_weight_explicit_beats_behavioral(self):
        values = ["always use BaseModel", "always use BaseModel"]
        explicit = compute_stats(_obs("style_base", values, ["explicit", "explicit"]))
        behavioral = compute_stats(_obs("style_base", values, ["behavioral", "behavioral"]))
        assert explicit[0]["confidence"] > behavioral[0]["confidence"]

    def test_deterministic_order(self):
        stats = compute_stats(_obs("a_pattern", ["b value"]) + _obs("b_pattern", ["a value"]))
        # Both count 1, same confidence → ordered by signature.
        assert stats[0]["signature"] == "a_pattern"
        assert stats[1]["signature"] == "b_pattern"


class TestEmergingCandidates:
    def test_threshold(self):
        strong = _obs("x", ["always x"] * 5, ["explicit"] * 5)
        weak = _obs("y", ["often y"] * 2, ["behavioral"] * 2)
        cands = emerging_candidates(compute_stats(strong + weak))
        sigs = {c["signature"] for c in cands}
        assert sigs == {"x"}


class TestBakeTick:
    def test_persists_emerging_candidates(self, temp_project_dir: Path):
        # 4 distinct values, one signature, all normalizing to one value key → count 4.
        records = [
            {"signature": "cmd_pnpm", "value": "always use pnpm install", "source": "explicit", "stack": "python"},
            {"signature": "cmd_pnpm", "value": "Always use pnpm install.", "source": "explicit", "stack": "python"},
            {"signature": "cmd_pnpm", "value": "always  use pnpm install!", "source": "explicit", "stack": "python"},
            {"signature": "cmd_pnpm", "value": "ALWAYS USE PNPM INSTALL", "source": "explicit", "stack": "python"},
        ]
        res = append_observations(workspace_path=temp_project_dir, records=records)
        assert res.get("appended") == 4

        tick = bake_tick(temp_project_dir)
        assert tick["success"] is True
        assert tick["changed"] is True
        assert tick["candidate_count"] == 1

        saved = read_baked(temp_project_dir)
        assert len(saved["candidates"]) == 1
        assert saved["candidates"][0]["signature"] == "cmd_pnpm"
        assert saved["candidates"][0]["value"] == "always use pnpm install"
        assert saved["candidates"][0]["confidence"] == round(0.8 * 0.9, 3)  # freq 0.8 × explicit 0.9

    def test_no_write_when_unchanged(self, temp_project_dir: Path):
        assert bake_tick(temp_project_dir)["changed"] is False
        assert not baked_path(temp_project_dir).exists()


class TestDelivery:
    def _seed_candidates(self, temp_project_dir: Path) -> None:
        records = [
            {"signature": "cmd_pnpm", "value": "always use pnpm install", "source": "explicit", "stack": "python"},
            {"signature": "cmd_pnpm", "value": "Always use pnpm install.", "source": "explicit", "stack": "python"},
            {"signature": "cmd_pnpm", "value": "always  use pnpm install!", "source": "explicit", "stack": "python"},
            {"signature": "cmd_pnpm", "value": "ALWAYS USE PNPM INSTALL", "source": "explicit", "stack": "python"},
        ]
        append_observations(workspace_path=temp_project_dir, records=records)
        bake_tick(temp_project_dir)

    def test_tells_once(self, temp_project_dir: Path):
        self._seed_candidates(temp_project_dir)
        first = deliver_candidates(temp_project_dir)
        assert len(first["pattern_candidates"]) == 1
        assert first["pattern_candidates"][0]["signature"] == "cmd_pnpm"
        # Second read → nothing new to tell (tell-once).
        second = deliver_candidates(temp_project_dir)
        assert second["pattern_candidates"] == []
        assert second["delivered"] == []

    def test_mark_delivered_persists(self, temp_project_dir: Path):
        self._seed_candidates(temp_project_dir)
        mark_delivered(temp_project_dir, ["cmd_pnpm"])
        assert "cmd_pnpm" in delivery_marker(temp_project_dir)
        assert undelivered_candidates(temp_project_dir) == []

    def test_scope_candidates_by_stack(self):
        cands = [
            {"signature": "a", "stack": "python"},
            {"signature": "b", "stack": "any"},
            {"signature": "c", "stack": "nodejs"},
        ]
        scoped = scope_candidates(cands, "python")
        assert {c["signature"] for c in scoped} == {"a", "b"}


class TestContextRender:
    def test_context_md_renders_patterns(self, tmp_path: Path):
        from mcp_server.helpers.context_builder import build_context_md

        md = build_context_md(
            tmp_path,
            patterns=[{"signature": "cmd_pnpm", "value": "always use pnpm install"}],
            pattern_candidates=[{"signature": "cmd_pnpm", "value": "always use pnpm install", "confidence": 0.72}],
        )
        assert "## Patterns" in md
        assert "New pattern candidates" in md
        assert "Baked patterns" in md
