"""
Baking pipeline — deterministic, LLM-free pattern extraction from observations.

Every ``action_call`` is a tick: **append → key → count → consistency → confidence →
re-evaluate → emit**. All steps are deterministic (no LLM); they read the observation
store (``.ai/memory-bank/observations.jsonl``) and persist emerging candidates to
``.ai/memory-bank/baked.json`` for delivery (Phase 5).

Keying is **generated, not per-stack hardcoded**: each raw signal value is normalized
into a canonical signature (case/whitespace/punctuation-insensitive), so "genuine
recurrence" is counted, not "times the server was woken".
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import settings
from .file_utils import write_file_safe
from .observation_store import read_observations

# ── Bake thresholds (calibrated so explicit/corrected emerge at ~4 obs, behavioral at ~5) ──
BAKE_MIN_COUNT = 2  # at least this many observations for a candidate
BAKE_MIN_CONSISTENCY = 0.5  # modal agreement within the cluster
BAKE_MIN_CONFIDENCE = 0.6  # confidence gate for an emerging candidate
BAKE_SATURATION = 5  # frequency reaches 1.0 at this many observations

# Source weights (explicit / corrected = strongest, inferred = weakest).
SOURCE_WEIGHT: dict[str, float] = {
    "explicit": 0.9,
    "corrected": 0.9,
    "behavioral": 0.6,
    "inferred": 0.4,
}
DEFAULT_SOURCE_WEIGHT = 0.4

_WORD_RE = re.compile(r"[a-z0-9]+")
_SPACE_RE = re.compile(r"\s+")
_TRAIL_NOISE_RE = re.compile(r"[\s.:;,!?]+$")


# ── Signature keying / normalization ─────────────────────────────────────────


def normalize_value(value: Any) -> str:
    """Canonical whitespace + case form of a raw signal value."""
    return _SPACE_RE.sub(" ", str(value or "").strip().lower())


def key_template(value: Any) -> str:
    """Key for instruction/template signals: case + punctuation + whitespace insensitive."""
    return _TRAIL_NOISE_RE.sub("", normalize_value(value))


def key_style(value: Any) -> str:
    """Key for style-fingerprint signals: word tokens only (case insensitive)."""
    return " ".join(_WORD_RE.findall(str(value or "").lower()))


def make_signature(value: Any) -> str:
    """Generate a deterministic signature key for a raw signal value.

    Template keying first (keeps the meaningful words, drops trailing noise);
    falls back to style keying when the value is only punctuation/symbols.
    """
    k = key_template(value)
    return k if k else key_style(value)


def normalize_signature(signature: Any) -> str:
    """Normalize a semantic signature key (case/whitespace/punctuation insensitive)."""
    return _TRAIL_NOISE_RE.sub("", normalize_value(signature))


def cluster_key(observation: dict[str, Any]) -> str:
    """Cluster key for an observation: its semantic ``signature`` if meaningful,
    else a generated key from the value. Signatures are the stable semantic key
    (agent-relayed or hook-generated); the generated value-key is the fallback.
    """
    sig = normalize_signature(observation.get("signature"))
    if sig:
        return sig
    return make_signature(observation.get("value"))


# ── Cluster stats ────────────────────────────────────────────────────────────


def _cluster_observations(observations: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group observations by their semantic signature (cluster) key."""
    clusters: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for o in observations:
        key = cluster_key(o)
        if key:
            clusters[key].append(o)
    return dict(clusters)


def compute_stats(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compute per-signature-cluster stats: count, consistency, confidence.

    Deterministic — repeated ticks over the same store produce identical output.

    - **count**: observations sharing the semantic signature key.
    - **consistency**: modal agreement of the *values* within the cluster (0..1) —
      a variance proxy (a cluster of near-identical values scores 1.0; a cluster of
      conflicting values scores lower).
    - **source_weight**: strongest source tier present in the cluster
      (explicit 0.9 / corrected 0.9 / behavioral 0.6 / inferred 0.4).
    - **confidence**: ``frequency × consistency × source_weight`` where frequency
      saturates to 1.0 at ``BAKE_SATURATION`` observations.
    """
    clusters = _cluster_observations(observations)
    stats: list[dict[str, Any]] = []
    for key, members in clusters.items():
        values = Counter(make_signature(m.get("value")) for m in members)
        top_key, top_count = values.most_common(1)[0]
        consistency = top_count / len(members)
        sources = [str(m.get("source") or "inferred") for m in members]
        source_weight = max(SOURCE_WEIGHT.get(s, DEFAULT_SOURCE_WEIGHT) for s in sources)
        count = len(members)
        freq_factor = min(1.0, count / BAKE_SATURATION)
        confidence = round(min(1.0, freq_factor * consistency * source_weight), 3)

        rep = next(
            (m.get("value") for m in members if make_signature(m.get("value")) == top_key),
            members[0].get("value"),
        )
        stats.append(
            {
                "signature": key,
                "count": count,
                "consistency": round(consistency, 3),
                "confidence": confidence,
                "source_weight": round(source_weight, 3),
                "sources": sorted(set(sources)),
                "stack": next((m.get("stack") for m in members if m.get("stack")), "any"),
                "project": next((m.get("project") for m in members if m.get("project")), ""),
                "value": rep,
                "last_ts": max((str(m.get("ts")) for m in members if m.get("ts")), default=""),
            }
        )
    # Deterministic order: highest confidence, then count, then signature.
    stats.sort(key=lambda s: (-s["confidence"], -s["count"], s["signature"]))
    return stats


def emerging_candidates(stats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter cluster stats to those that crossed the bake threshold."""
    return [
        s
        for s in stats
        if s["count"] >= BAKE_MIN_COUNT
        and s["consistency"] >= BAKE_MIN_CONSISTENCY
        and s["confidence"] >= BAKE_MIN_CONFIDENCE
    ]


# ── Persistence (baked.json) ─────────────────────────────────────────────────


def baked_path(workspace_path: str | Path) -> Path:
    """Location of the baked-candidates file (``.ai/memory-bank/baked.json``)."""
    return settings.get_memory_bank_dir(workspace_path) / "baked.json"


def read_baked(workspace_path: str | Path) -> dict[str, Any]:
    """Read the baked candidates + delivery marker; tolerant of a missing/corrupt file."""
    path = baked_path(workspace_path)
    if path.exists():
        try:
            data = json.loads(path.read_text("utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:  # noqa: BLE001 — torn/corrupt baked file → fresh state
            pass
    return {"candidates": [], "delivery": {}}


def bake(workspace_path: str | Path) -> dict[str, Any]:
    """Run the full bake: read observations → key → count/consistency → confidence → candidates."""
    observations = read_observations(workspace_path)
    stats = compute_stats(observations)
    candidates = emerging_candidates(stats)
    return {
        "success": True,
        "total_observations": len(observations),
        "clusters": len(stats),
        "stats": stats,
        "candidates": candidates,
    }


def bake_tick(workspace_path: str | Path) -> dict[str, Any]:
    """Run the bake + persist emerging candidates (atomic) when they change.

    Cheap per call: one read + one count; only writes ``baked.json`` when the
    candidate set actually changed. Never raises.
    """
    result = bake(workspace_path)
    candidates = result["candidates"]
    prev = read_baked(workspace_path)
    changed = prev.get("candidates", []) != candidates
    if changed:
        write_file_safe(
            baked_path(workspace_path),
            json.dumps(
                {"candidates": candidates, "delivery": prev.get("delivery", {})},
                indent=2,
                ensure_ascii=False,
            ),
        )
    result["changed"] = changed
    result["candidate_count"] = len(candidates)
    return result


# ── Delivery (Phase 5) — tell-once via the delivery marker ──────────────────


def delivery_marker(workspace_path: str | Path) -> dict[str, str]:
    """The delivery map ``{signature: delivered_at_ts}`` from baked.json."""
    return dict(read_baked(workspace_path).get("delivery") or {})


def undelivered_candidates(workspace_path: str | Path) -> list[dict[str, Any]]:
    """Candidates whose signature is NOT in the delivery marker (not yet told)."""
    baked = read_baked(workspace_path)
    delivery = baked.get("delivery") or {}
    return [c for c in (baked.get("candidates") or []) if c.get("signature") not in delivery]


def mark_delivered(workspace_path: str | Path, signatures: list[str]) -> dict[str, str]:
    """Record delivered signatures (tell-once) in baked.json. Returns the new delivery map."""
    baked = read_baked(workspace_path)
    delivery = dict(baked.get("delivery") or {})
    now = datetime.now(timezone.utc).isoformat()
    changed = False
    for sig in signatures:
        if sig and sig not in delivery:
            delivery[sig] = now
            changed = True
    if changed:
        write_file_safe(
            baked_path(workspace_path),
            json.dumps(
                {"candidates": baked.get("candidates") or [], "delivery": delivery},
                indent=2,
                ensure_ascii=False,
            ),
        )
    return delivery


def deliver_candidates(workspace_path: str | Path) -> dict[str, Any]:
    """Return not-yet-told candidates AND mark them delivered (tell-once).

    The act of returning candidates to the agent counts as telling them — so the
    delivery marker is advanced here, and the next read sees an empty candidate
    list (zero noise until genuinely new patterns bake).
    """
    undelivered = undelivered_candidates(workspace_path)
    mark_delivered(workspace_path, [str(c.get("signature")) for c in undelivered])
    return {
        "pattern_candidates": undelivered,
        "delivered": [str(c.get("signature")) for c in undelivered],
    }


# ── Read-side injection (Phase 5) — stack scoping ───────────────────────────


def detect_stack(workspace_path: str | Path) -> str:
    """Detect the project stack (framework → language → 'any'), same as mem_search."""
    try:
        from ..tools.context_tools.scanner import detect_framework

        info = detect_framework(str(workspace_path))
        fw = info.get("framework")
        if fw and fw != "Unknown":
            return fw
        langs = (info.get("all_detected") or {}).get("languages", [])
        if langs:
            return langs[0]
    except Exception:  # noqa: BLE001 — best-effort
        pass
    return "any"


def scope_candidates(candidates: list[dict[str, Any]], stack: str) -> list[dict[str, Any]]:
    """Keep portable (``any``) candidates plus those matching the workspace stack."""
    out = []
    for c in candidates:
        s = str(c.get("stack") or "any")
        if s in ("", "any") or s == stack:
            out.append(c)
    return out


def should_spawn_subagent(workspace_path: str | Path, max_candidates: int = 3) -> dict[str, Any]:
    """Gate the baking-subagent spawn (three-tier orchestration — token-cost control).

    Spawn a baking subagent ONLY when the delivery marker shows NEW, not-yet-told
    candidates (genuinely new baked patterns worth distilling). Returns
    ``{should_spawn, undelivered_count, candidates}`` (candidates capped).
    """
    undelivered = undelivered_candidates(workspace_path)
    if not undelivered:
        return {"should_spawn": False, "undelivered_count": 0, "candidates": []}
    return {
        "should_spawn": True,
        "undelivered_count": len(undelivered),
        "candidates": undelivered[:max_candidates],
    }
