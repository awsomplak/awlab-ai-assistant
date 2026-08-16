"""
Observation store — the pattern-baking evidence log.

One observation per line (JSONL) at ``.ai/memory-bank/observations.jsonl``, mirroring the
``pending.jsonl`` offline-cache pattern (atomic append, torn-tail tolerant reader). Each
observation is a raw signal (a command, a user instruction, a code-style fact) that the
baking pipeline later keys → counts → measures consistency → computes confidence.

Ingestion classes:
- agent-relayed (``mem_observe``) — chat/behavior signals the agent relays.
- hook-derived (``hook --agent``) — host lifecycle events capture.
- server-mined — per-call re-scan of code stats/git (dedup-guarded).

The dedup/delta guard (fingerprint) prevents re-scans from double-counting: only NEW or
CHANGED signals are appended, so "count = genuine recurrence", not "count = times woken".
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import settings

# ── Path ─────────────────────────────────────────────────────────────────────


def observations_path(workspace_path: str | Path) -> Path:
    """Location of the observation log (``.ai/memory-bank/observations.jsonl``)."""
    return settings.get_memory_bank_dir(workspace_path) / "observations.jsonl"


# ── Record shape ─────────────────────────────────────────────────────────────

_OBS_FIELDS = {
    "signature",  # normalized key (command / instruction template / style fingerprint)
    "value",  # the raw signal (e.g. "npm install", "always use pnpm")
    "ts",  # ISO timestamp (UTC)
    "source",  # explicit | corrected | behavioral | inferred | mined
    "stack",  # detected stack (Python / Laravel / any) — tag, not detection input
    "project",  # project_id slug
    "context",  # optional area (workflow / models / commands)
    "fingerprint",  # dedup guard: hash of path+mtime+stat for mined signals, else hash(value)
}


def _fingerprint(value: str, signature: str = "", path: str = "", mtime: float = 0.0, stat: str = "") -> str:
    """Dedup/delta fingerprint for a signal.

    For server-mined signals (path+mtime+stat) a change in the source invalidates the
    old fingerprint. For value-only signals the fingerprint includes the signature (the
    semantic key), so the same value under a different pattern signature is distinct.
    """
    h = hashlib.sha256()
    h.update(value.encode("utf-8"))
    if not path:
        # Agent-relayed / value-only: signature is the discriminator.
        h.update(b"|")
        h.update(signature.encode("utf-8"))
    else:
        h.update(b"|")
        h.update(path.encode("utf-8"))
        h.update(b"|")
        h.update(f"{mtime:.6f}".encode("utf-8"))
        h.update(b"|")
        h.update(stat.encode("utf-8"))
    return h.hexdigest()[:32]


def build_record(
    signature: str,
    value: str,
    *,
    source: str = "inferred",
    stack: str = "any",
    project: str = "",
    context: str = "",
    path: str = "",
    mtime: float = 0.0,
    stat: str = "",
    ts: str | None = None,
) -> dict[str, Any]:
    """Build a well-formed observation record."""
    return {
        "signature": signature,
        "value": value,
        "ts": ts or datetime.now(timezone.utc).isoformat(),
        "source": source,
        "stack": stack,
        "project": project,
        "context": context,
        "fingerprint": _fingerprint(value, signature=signature, path=path, mtime=mtime, stat=stat),
    }


def normalize_record(rec: dict[str, Any]) -> dict[str, Any]:
    """Coerce a possibly-raw record into the canonical shape (missing → defaults)."""
    sig = str(rec.get("signature") or "").strip()
    value = str(rec.get("value") or "").strip()
    if not sig or not value:
        return {}
    return {
        "signature": sig,
        "value": value,
        "ts": str(rec.get("ts") or datetime.now(timezone.utc).isoformat()),
        "source": str(rec.get("source") or "inferred"),
        "stack": str(rec.get("stack") or "any"),
        "project": str(rec.get("project") or ""),
        "context": str(rec.get("context") or ""),
        "fingerprint": str(
            rec.get("fingerprint")
            or _fingerprint(
                value,
                signature=sig,
                path=str(rec.get("path") or ""),
                mtime=float(rec.get("mtime") or 0.0),
                stat=str(rec.get("stat") or ""),
            )
        ),
    }


# ── Append (atomic, dedup-guarded) ───────────────────────────────────────────


def append_observations(workspace_path: str | Path, records: list[dict[str, Any]]) -> dict[str, Any]:
    """Append observations (dedup/delta-guarded).

    Returns ``{success, appended, skipped_duplicates, skipped_invalid}``.
    """
    path = observations_path(workspace_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        return {"success": False, "error": "cannot create memory-bank dir"}

    existing = _existing_fingerprints(path)
    appended = 0
    dup = 0
    invalid = 0

    try:
        with open(path, "a", encoding="utf-8") as f:
            for raw in records:
                rec = normalize_record(raw)
                if not rec:
                    invalid += 1
                    continue
                fp = rec["fingerprint"]
                if fp in existing:
                    dup += 1
                    continue  # dedup/delta guard — already recorded
                f.write(json.dumps(rec, default=str) + "\n")
                existing.add(fp)
                appended += 1
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": str(e)}

    return {"success": True, "appended": appended, "skipped_duplicates": dup, "skipped_invalid": invalid}


def _existing_fingerprints(path: Path) -> set[str]:
    """All fingerprints already in the log (torn-tail tolerant)."""
    out: set[str] = set()
    try:
        if not path.exists():
            return out
        for line in path.read_text("utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if isinstance(obj, dict) and obj.get("fingerprint"):
                out.add(str(obj["fingerprint"]))
    except Exception:
        pass
    return out


# ── Read ─────────────────────────────────────────────────────────────────────


def read_observations(workspace_path: str | Path) -> list[dict[str, Any]]:
    """Read all observations (one JSON object per line; corrupt lines skipped)."""
    try:
        path = observations_path(workspace_path)
        if not path.exists():
            return []
        out: list[dict[str, Any]] = []
        for line in path.read_text("utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue  # tolerate a torn/corrupt tail line
            if isinstance(obj, dict):
                out.append(obj)
        return out
    except Exception:
        return []


def clear_observations(workspace_path: str | Path) -> bool:
    """Remove the observation log (test/scratch utility)."""
    try:
        path = observations_path(workspace_path)
        if path.exists():
            path.unlink()
        return True
    except Exception:
        return False
