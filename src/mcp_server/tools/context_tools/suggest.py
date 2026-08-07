"""
Intelligent file suggestions for a task description.

Uses project fingerprint, Quick Index, and memory search to suggest
the most relevant file paths for a given task.
"""

import re
from pathlib import Path
from typing import Any

from ...helpers.agent_recall import search_nodes as _search_nodes
from .scanner import scan_project

# ── Scoring Helpers ──────────────────────────────────────────────────────────


def _score_entry_points(
    entry_points: dict[str, list[str]],
    task_words: set[str],
) -> list[dict[str, Any]]:
    """Score files from entry points against task description keywords."""
    candidates: list[dict[str, Any]] = []
    for target_dir, files in entry_points.items():
        for filepath in files:
            score = 0.0
            score += 0.1  # File in a relevant target directory
            filename = Path(filepath).stem.lower()
            file_parts = set(re.split(r"[_\-.]", filename))
            matched = task_words & file_parts
            score += len(matched) * 0.3
            ext = Path(filepath).suffix.lower()
            if ext in {".py", ".js", ".ts", ".jsx", ".tsx", ".php", ".rb"}:
                score += 0.1
            candidates.append(
                {
                    "path": filepath,
                    "reason": (
                        f"Part of {target_dir} target directory"
                        if len(matched) == 0
                        else f"Matches task keywords: {', '.join(matched)}"
                    ),
                    "score": round(min(1.0, score), 2),
                }
            )
    return candidates


def _score_relationships(
    relationships: list[dict[str, Any]],
    task_words: set[str],
) -> list[dict[str, Any]]:
    """Score files that import modules related to task description."""
    candidates: list[dict[str, Any]] = []
    for rel in relationships:
        rel_file = rel.get("file", "")
        for imp in rel.get("imports", []):
            if any(word in imp.lower() for word in task_words):
                candidates.append(
                    {
                        "path": rel_file,
                        "reason": f"Imports module related to task: {imp}",
                        "score": 0.5,
                    }
                )
    return candidates


def _score_memory_results(
    memory_results: list | None,
    task_words: set[str],
) -> list[dict[str, Any]]:
    """Score memory entities related to task description."""
    candidates: list[dict[str, Any]] = []
    if not isinstance(memory_results, list):
        return candidates
    for entity in memory_results:
        if not isinstance(entity, dict):
            continue
        name = entity.get("name", "")
        observations = entity.get("observations") or entity.get("contents") or []
        if isinstance(observations, str):
            observations = [observations]
        for obs in observations[:2]:
            if isinstance(obs, str) and any(word in obs.lower() for word in task_words):
                candidates.append(
                    {
                        "path": f"memory:{name}",
                        "reason": obs[:100],
                        "score": 0.4,
                    }
                )
    return candidates


def _deduplicate_and_sort(
    candidates: list[dict[str, Any]],
    top_n: int = 3,
) -> list[dict[str, Any]]:
    """Deduplicate candidates by path (keep highest score) and sort descending."""
    seen: dict[str, dict[str, Any]] = {}
    for c in candidates:
        p = c["path"]
        if p in seen:
            if c["score"] > seen[p]["score"]:
                seen[p] = c
        else:
            seen[p] = c
    return sorted(seen.values(), key=lambda x: x["score"], reverse=True)[:top_n]


# ── Main Public API ─────────────────────────────────────────────────────────


async def suggest_relevant_files(
    workspace_path: str | Path,
    task_description: str,
) -> dict[str, Any]:
    """
    Use project fingerprint + Quick Index + memory search to suggest
    the top 3 file paths most likely needed for a given task.

    Args:
        task_description: Description of the task to find relevant files for.
        workspace_path: Project root path. If empty, falls back to CWD.

    Returns:
        { success, suggestions: [{path, reason, score}], task_description }
    """
    task_words = set(task_description.lower().split())

    # 1. Get project fingerprint (cached)
    try:
        fingerprint = await scan_project(workspace_path)
    except Exception as e:
        return {"success": False, "error": str(e), "suggestions": [], "task_description": task_description}

    entry_points = fingerprint.get("entry_points", {})
    relationships = fingerprint.get("relationships", [])

    # 2. Search memory for related patterns
    try:
        memory_results = _search_nodes(workspace_path, query=task_description, limit=5)
    except Exception:
        memory_results = []

    # 3. Score files from three orthogonal signals
    candidates: list[dict[str, Any]] = []
    candidates.extend(_score_entry_points(entry_points, task_words))
    candidates.extend(_score_relationships(relationships, task_words))
    candidates.extend(_score_memory_results(memory_results, task_words))

    # 4. Deduplicate, sort, take top 3
    top = _deduplicate_and_sort(candidates, top_n=3)

    return {
        "success": True,
        "suggestions": top,
        "task_description": task_description,
        "total_candidates": len({c["path"] for c in candidates}),
    }
