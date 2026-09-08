"""
Direct Python bridge to the agent-recall pip package (v0.4.0+).

Uses MCPBridge from agent_recall.mcp_bridge instead of subprocess CLI calls.
Supports two project isolation strategies:

1. **Per-project DB files** (backward-compatible with agent_recall_bridge.py):
   If ``project_id`` is provided to any function (or via the ``create_bridge``
   factory), a dedicated SQLite database ``memory_{sanitized_id}.db`` is used,
   with no scope isolation inside the bridge.

2. **Single-DB scope isolation** (default when no project_id is given):
   A single ``memory.db`` database is used, and memories are isolated via
   the scope chain ``["global", scope]``.
"""

import json
import os
import platform
import re
import sqlite3
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from agent_recall import MCPBridge, MemoryConfig

from ..config import settings
from .logger import logger
from .workspace import resolve_db_path

_SQLITE_PATCHED = False


def _patch_sqlite_for_crsqlite() -> None:
    global _SQLITE_PATCHED
    if _SQLITE_PATCHED:
        return
    _SQLITE_PATCHED = True

    _orig_connect = sqlite3.connect

    def _cr_connect(*args, **kwargs):
        conn = _orig_connect(*args, **kwargs)

        system = platform.system().lower()
        if system == "windows":
            ext = ".dll"
        elif system == "darwin":
            ext = ".dylib"
        else:
            ext = ".so"

        cr_path = settings.config_home / "extensions" / f"crsqlite{ext}"
        if cr_path.exists():
            try:
                conn.enable_load_extension(True)
                conn.load_extension(str(cr_path))
                conn.enable_load_extension(False)
            except Exception as e:
                logger.warning(f"Failed to load cr-sqlite extension from {cr_path}: {e}")

        return conn

    sqlite3.connect = _cr_connect


# ── Helpers ─────────────────────────────────────────────────────────────────


def _get_scope(workspace_path: str | Path, project_id: str | None = None) -> str:
    """
    Return the project scope slug derived from project-id or directory name.

    Args:
        workspace_path: Absolute path to the project workspace root.
        project_id: Optional explicit project identifier. When provided, uses
                    a dedicated per-project database file with no scope chain
                    (backward-compatible with ``agent_recall_bridge``).
                    When None, uses scope isolation within a shared database.
    """
    if project_id:
        return project_id
    pid = settings.get_project_id(workspace_path)
    if pid:
        return pid

    return settings._resolve_workspace(workspace_path).name.lower().replace(" ", "_").replace("-", "_")


# ── SQLite optimisations ────────────────────────────────────────────────────
_wal_enabled: set[str] = set()
_wal_lock = threading.Lock()


def _enable_wal_mode(db_path: str) -> None:
    """Enable WAL journal mode and busytimeout for concurrent access."""
    if db_path in _wal_enabled:
        return
    with _wal_lock:
        if db_path in _wal_enabled:
            return
        try:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(db_path, timeout=3)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=3000;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.close()
            _wal_enabled.add(db_path)
        except sqlite3.Error as e:
            logger.warning(f"Could not set WAL mode on {db_path}: {e}")


# ── Bridge lifecycle ─────────────────────────────────────────────────────────


def user_patterns_db_path() -> str:
    """Dedicated user-patterns store (cross-project), separate from project memory.db.

    Kept as its own file so cross-project learned patterns are stable, auditable,
    and independent of any project's memory/cleanup — the agent "grows with user".
    """
    return str(settings.config_home / "memory" / "user_patterns.db")


# ── Project families (correlated projects at different paths) ────────────────


def project_families_path() -> Path:
    """Family declaration file: ``~/.awlab-id/agent-memory/project-families.json``.

    Recommended shape: ``{family_slug: {"name": str, "members": [{"path", "project_id"}]}}``
    (v1 array-of-paths is still accepted).``project_id`` may contain ``-``/``_``;
    the project's own ``.ai/project-id`` is authoritative over it (see
    :func:`sync_family_project_ids`).
    """
    return settings.config_home / "project-families.json"


def _load_families() -> dict[str, dict]:
    """Load families as ``{slug: {"name": str, "members": [{"path", "project_id"}]}}``.

    v2 shape (recommended): ``{slug: {"name"?: str, "members": [{"path", "project_id"}]}}``
    v1 shape (compat):      ``{slug: [member_path, ...]}`` → project_id derived later.
    Returns {} on missing/corrupt (never breaks the server). ``project_id`` must be
    unique within a family (duplicates dropped).
    """

    try:
        path = project_families_path()
        if not path.exists():
            return {}
        raw = json.loads(path.read_text("utf-8"))
        if not isinstance(raw, dict):
            return {}
        out: dict[str, dict] = {}
        for slug, v in raw.items():
            if not isinstance(slug, str) or not slug:
                continue
            name = ""
            members: list[dict[str, str]] = []
            if isinstance(v, dict):
                name = str(v.get("name") or "")
                mlist = v.get("members")
                if isinstance(mlist, list):
                    seen_ids: set[str] = set()
                    for m in mlist:
                        if not isinstance(m, dict) or not m.get("path"):
                            continue
                        p = str(m["path"])
                        pid = str(m.get("project_id") or "").strip()
                        if pid and pid in seen_ids:
                            continue  # project_id must be unique within a family
                        if pid:
                            seen_ids.add(pid)
                        members.append({"path": p, "project_id": pid})
            elif isinstance(v, list):
                for p in v:
                    if isinstance(p, str) and p:
                        members.append({"path": p, "project_id": ""})
            if members:
                out[slug] = {"name": name, "members": members}
        return out
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        return {}


def family_slugs() -> list[str]:
    """All declared family slugs."""
    return sorted(_load_families())


def family_members(slug: str) -> list[str]:
    """Member workspace paths for a family slug ([] if unknown)."""
    return [m["path"] for m in _load_families().get(slug, {}).get("members", [])]


def family_member_project_ids(slug: str) -> dict[str, str]:
    """``{member_path: declared project_id}`` for a family (only declared, non-empty ids).

    Member paths are normalized via ``Path.resolve()`` so lookups are
    separator-agnostic — ``D:/Project/Foo`` in the JSON equals ``D:\\Project\\Foo``
    on disk (live family files commonly use forward slashes).
    """
    out: dict[str, str] = {}
    for m in _load_families().get(slug, {}).get("members", []):
        pid = m.get("project_id")
        if not pid:
            continue
        try:
            key = str(Path(m["path"]).resolve())
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
            key = str(m["path"])
        out[key] = pid
    return out


def _slugify(name: str) -> str:
    """Directory/name → lowercase slug for derived identities."""
    return re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_").lower() or "project"


def family_member_id(slug: str, workspace_path: str | Path) -> str:
    """Stable member identity: project's own ``.ai/project-id`` (authoritative) > declared > ``<slug>-<dir>``.

    The project file wins once present — the path is correct and only the id may
    differ (e.g. hyphen vs underscore); :func:`sync_family_project_ids` reconciles
    the family JSON to it. Used as the family graph ``repo::`` tag + seeded id.
    """
    try:
        wp = str(Path(workspace_path).resolve())
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        wp = str(workspace_path)
    try:
        pid_file = Path(workspace_path) / ".ai" / "project-id"
        if pid_file.is_file():
            pid = pid_file.read_text(encoding="utf-8").strip()
            if pid:
                return pid
    except OSError:
        pass
    declared = family_member_project_ids(slug).get(wp, "")
    if declared:
        return declared
    return f"{slug}-{_slugify(Path(workspace_path).name)}"


def seed_member_project_id(slug: str, workspace_path: str | Path) -> str:
    """Resolve a member's project_id and SEED ``.ai/project-id`` if the project lacks one.

    Tiny identity marker only (not graph output) — a fresh project becomes
    self-identifying for future per-project memory/graph scoping.
    """
    pid = family_member_id(slug, workspace_path)
    try:
        pid_file = Path(workspace_path) / ".ai" / "project-id"
        if not pid_file.is_file():
            pid_file.parent.mkdir(parents=True, exist_ok=True)
            pid_file.write_text(pid, encoding="utf-8")
    except OSError:
        pass
    return pid


def sync_family_project_ids(slug: str) -> dict:
    """Reconcile the family JSON's member ``project_id``s to each project's own id.

    The project's ``.ai/project-id`` is authoritative once present — the path is
    correct and only the id may differ (e.g. ``eka-warehouse`` vs ``eka_warehouse``).
    This updates the family JSON to match, then runs a DUPLICATE CHECKER: if two
    members end up with the same ``project_id`` but different paths, the later one
    loses its declared id (so it derives a distinct ``<slug>-<dir>`` id).

    Returns ``{updated, changes, conflicts}`` (never raises; writes atomically).
    """
    path = project_families_path()
    try:
        raw = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        return {"updated": False, "changes": [], "conflicts": []}
    fam = raw.get(slug)
    members = fam.get("members") if isinstance(fam, dict) else None
    if not isinstance(members, list):
        return {"updated": False, "changes": [], "conflicts": []}

    changes: list[str] = []
    conflicts: list[str] = []
    seen_ids: dict[str, str] = {}  # project_id -> path
    for m in members:
        if not isinstance(m, dict) or not m.get("path"):
            continue
        p = str(m["path"])
        file_pid = ""
        try:
            pf = Path(p) / ".ai" / "project-id"
            if pf.is_file():
                file_pid = pf.read_text(encoding="utf-8").strip()
        except OSError:
            pass
        declared = str(m.get("project_id") or "").strip()
        if file_pid and declared and file_pid != declared:
            m["project_id"] = file_pid
            changes.append(f"{p}: {declared!r} -> {file_pid!r}")
        effective = file_pid or declared
        if effective and effective in seen_ids and seen_ids[effective] != p:
            m["project_id"] = ""
            conflicts.append(
                f"duplicate project_id '{effective}' ({seen_ids[effective]} vs {p}) — "
                "cleared so it derives a distinct id"
            )
        elif effective:
            seen_ids[effective] = p

    if not changes and not conflicts:
        return {"updated": False, "changes": [], "conflicts": []}
    try:
        path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        return {"updated": True, "changes": changes, "conflicts": conflicts}
    except OSError:
        return {"updated": False, "changes": changes, "conflicts": conflicts}


# ── Family config writes (agent-managed project-families.json) ──────────────

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _normalize_family_path(p: str) -> str:
    """Normalize a member path to an absolute, forward-slash string."""
    try:
        return str(Path(p).expanduser().resolve()).replace("\\", "/")
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        return str(p).replace("\\", "/")


def validate_family_slug(slug: str) -> str | None:
    """Validate a family slug. Returns an error message, or None if valid.

    Rules: non-empty, lowercase letters / digits / ``_`` / ``-``, starts with a
    letter or digit. Matches the ``family_<slug>`` store pattern enforced by the
    memory action params (``family_[a-z0-9_-]+``).
    """
    if not isinstance(slug, str) or not slug:
        return "family slug is required (lowercase letters/digits/_/-)"
    if not _SLUG_RE.match(slug):
        return (
            f"invalid family slug {slug!r}: use lowercase letters, digits, '_' or '-' (must start with a letter/digit)"  # noqa: E501
        )
    return None


def _coerce_members(members: list | None) -> tuple[list[dict], list[str]]:
    """Normalize + validate a member list → ``(members, errors)``.

    Each member is ``{"path": str, "project_id": str}``. Rules enforced:
    absolute path (normalized to forward slashes), non-empty slug-safe
    ``project_id`` when given, no duplicate paths, no duplicate ``project_id``.
    """
    out: list[dict] = []
    errors: list[str] = []
    seen_paths: dict[str, str] = {}
    seen_ids: dict[str, str] = {}
    for i, m in enumerate(members or []):
        if not isinstance(m, dict):
            errors.append(f"member[{i}]: must be an object {{path, project_id?}}")
            continue
        raw_path = str(m.get("path") or "").strip()
        if not raw_path:
            errors.append(f"member[{i}]: 'path' is required")
            continue
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            errors.append(f"member[{i}]: path must be absolute, got {raw_path!r}")
            continue
        norm = _normalize_family_path(raw_path)
        pid = str(m.get("project_id") or "").strip()
        if pid and not re.match(r"^[A-Za-z0-9][A-Za-z0-9_-]*$", pid):
            errors.append(f"member[{i}]: invalid project_id {pid!r} (letters/digits/_/- only)")
            continue
        if norm in seen_paths:
            errors.append(f"member[{i}]: duplicate member path {norm!r}")
            continue
        if pid and pid in seen_ids:
            errors.append(f"member[{i}]: duplicate project_id {pid!r} within this family")
            continue
        seen_paths[norm] = raw_path
        if pid:
            seen_ids[pid] = norm
        out.append({"path": norm, "project_id": pid})
    return out, errors


def write_families_json(families: dict) -> dict:
    """Atomically write the whole ``project-families.json`` (v2 shape).

    Writes temp + ``os.replace`` so a crash never leaves a partial file.
    Returns ``{success, path, error?}`` (never raises).
    """
    path = project_families_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".project-families.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(families, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.replace(tmp, str(path))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return {"success": True, "path": str(path)}
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as e:
        return {"success": False, "path": str(path), "error": str(e)}


def upsert_family(
    slug: str,
    name: str | None = None,
    members: list | None = None,
    replace_members: bool = False,
) -> dict:
    """Create or update a family entry in ``project-families.json``.

    - ``name``: updates the human-readable name (keeps existing when omitted).
    - ``members``: upserted by normalized path — a matching path updates its
      ``project_id``, a new path is appended. When ``replace_members`` is True the
      existing member list is replaced entirely (must still pass validation).
    - Always writes the v2 shape. Returns ``{success, action, slug, name,
      members, changed, errors}``.
    """
    slug_err = validate_family_slug(slug)
    if slug_err:
        return {"success": False, "error": slug_err, "action": "invalid", "slug": slug}
    norm_members, m_errors = _coerce_members(members or [])
    if m_errors:
        return {"success": False, "error": "; ".join(m_errors[:5]), "action": "invalid", "slug": slug}

    families = _load_families()  # normalizes v1→v2 on read
    existed = slug in families
    if not existed:
        families[slug] = {"name": name or "", "members": []}
    fam = families[slug]
    if name is not None:
        fam["name"] = name
    if replace_members:
        fam["members"] = []
    # Merge by normalized path (upsert).
    by_path = {_normalize_family_path(m["path"]): m for m in fam.get("members", [])}
    changed: list[str] = []
    for m in norm_members:
        existing = by_path.get(m["path"])
        if existing is None:
            by_path[m["path"]] = dict(m)
            changed.append(f"+ {m['path']}")
        elif existing.get("project_id") != m.get("project_id"):
            existing["project_id"] = m.get("project_id")
            changed.append(f"~ {m['path']} project_id -> {m.get('project_id') or '(none)'}")
    fam["members"] = list(by_path.values())

    write = write_families_json(families)
    if not write["success"]:
        return {"success": False, "error": write.get("error", "write failed"), "action": "write", "slug": slug}
    return {
        "success": True,
        "action": "updated" if existed else "created",
        "slug": slug,
        "name": fam.get("name", ""),
        "members": fam["members"],
        "changed": changed,
        "errors": [],
        "path": write["path"],
    }


def remove_family(slug: str) -> dict:
    """Remove a whole family from ``project-families.json``. Returns ``{success, removed}``."""
    if validate_family_slug(slug):
        return {"success": False, "error": "invalid family slug", "removed": False}
    families = _load_families()
    if slug not in families:
        return {"success": False, "error": f"family '{slug}' not found", "removed": False}
    families.pop(slug)
    write = write_families_json(families)
    if not write["success"]:
        return {"success": False, "error": write.get("error", "write failed"), "removed": False}
    return {"success": True, "removed": True, "slug": slug, "path": write["path"]}


def remove_family_member(slug: str, member_path: str) -> dict:
    """Remove one member (by path) from a family. Returns ``{success, removed, remaining}``."""
    if validate_family_slug(slug):
        return {"success": False, "error": "invalid family slug", "removed": False}
    if not member_path:
        return {"success": False, "error": "member path is required", "removed": False}
    target = _normalize_family_path(member_path)
    families = _load_families()
    fam = families.get(slug)
    if not fam:
        return {"success": False, "error": f"family '{slug}' not found", "removed": False}
    members = fam.get("members", [])
    kept = [m for m in members if _normalize_family_path(m.get("path", "")) != target]
    if len(kept) == len(members):
        return {"success": False, "error": f"member {target!r} not in family '{slug}'", "removed": False}
    fam["members"] = kept
    write = write_families_json(families)
    if not write["success"]:
        return {"success": False, "error": write.get("error", "write failed"), "removed": False}
    return {
        "success": True,
        "removed": True,
        "slug": slug,
        "path": target,
        "remaining": fam["members"],
        "config_path": write["path"],
    }


def resolve_family_info(workspace_path: str | Path | None = None, slug: str | None = None) -> dict:
    """Resolve a family (or all families) into a ready-to-consume info payload.

    Used by the read-only ``family_info`` action. When ``slug`` is given returns
    only that family (or an error if unknown). Otherwise returns every declared
    family plus, when ``workspace_path`` is given:
    - ``workspace_families`` — EVERY family containing this workspace (multi-family safe)
    - ``workspace_family`` — the PRIMARY family (first declared match; == the
      ``.ai/family-id`` marker content) or None
    - ``family_id`` — the current ``.ai/family-id`` marker content (synced to the
      primary family before returning, so it is never stale)

    ``store`` is the memory-store name (``family_<slug>``) for each family.
    """
    families = _load_families()
    all_families = [
        {
            "slug": s,
            "name": f.get("name", ""),
            "store": f"family_{s}",
            "members": f.get("members", []),
        }
        for s, f in sorted(families.items())
    ]
    if slug is not None:
        err = validate_family_slug(slug)
        if err:
            return {"success": False, "error": err}
        match = next((f for f in all_families if f["slug"] == slug), None)
        if match is None:
            return {"success": False, "error": f"family '{slug}' is not declared in project-families.json"}
        return {"success": True, "family": match, "families": all_families}

    workspace_families: list[dict] = []
    primary_slug: str | None = None
    if workspace_path:
        # Recomputed-on-read marker sync keeps .ai/family-id fresh (no cache).
        primary_slug = sync_family_id(workspace_path).get("family_id")
        by_slug = {f["slug"]: f for f in all_families}
        workspace_families = [by_slug[s] for s in families_for_workspace(workspace_path) if s in by_slug]
    return {
        "success": True,
        "family_id": primary_slug,
        "workspace_family": next((f for f in workspace_families if f["slug"] == primary_slug), None)
        if workspace_families
        else None,
        "workspace_families": workspace_families,
        "families": all_families,
    }


def families_for_workspace(workspace_path: str | Path | None) -> list[str]:
    """ALL family slugs whose members include this workspace path (multi-family safe).

    A single project may belong to several families; this returns every match in
    declaration order. ``family_for_workspace`` is the first of these (the PRIMARY).
    """
    if not workspace_path:
        return []
    try:
        wp = str(Path(workspace_path).resolve())
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        return []
    slugs: list[str] = []
    for slug, fam in _load_families().items():
        for m in fam.get("members", []):
            try:
                if str(Path(m["path"]).resolve()) == wp:
                    slugs.append(slug)
                    break
            except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
                continue
    return slugs


def family_for_workspace(workspace_path: str | Path | None) -> str | None:
    """Return the PRIMARY family slug containing this workspace path, if any.

    Primary = first declared family in ``project-families.json`` that lists this
    path (deterministic). For ALL families use :func:`families_for_workspace`.
    """
    slugs = families_for_workspace(workspace_path)
    return slugs[0] if slugs else None


# ── Per-project family marker (.ai/family-id) — mirrors .ai/project-id ──────


def family_id_path(workspace_path: str | Path) -> Path:
    """Per-project marker file: ``<workspace>/.ai/family-id`` (single-line slug)."""
    return Path(workspace_path).resolve() / ".ai" / "family-id"


def read_family_id(workspace_path: str | Path) -> str | None:
    """Read the raw ``.ai/family-id`` content (first non-empty line), or None."""
    path = family_id_path(workspace_path)
    if path.is_file():
        try:
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return text.splitlines()[0].strip()
        except OSError:
            pass
    return None


def sync_family_id(workspace_path: str | Path) -> dict:
    """Recompute the PRIMARY family and reconcile ``.ai/family-id`` to it.

    Recompute-on-read freshness model: every discovery call re-derives the primary
    from the global config and rewrites the marker only when it changed (or is
    missing). A stale marker is corrected; a project that left every family gets
    its marker removed. Returns ``{success, family_id, changed, path}``.
    """
    path = family_id_path(workspace_path)
    slug = family_for_workspace(workspace_path)
    try:
        if slug:
            path.parent.mkdir(parents=True, exist_ok=True)
            changed = read_family_id(workspace_path) != slug
            if changed:
                path.write_text(slug + "\n", encoding="utf-8")
            return {"success": True, "family_id": slug, "changed": changed, "path": str(path)}
        if path.exists():
            path.unlink()
            return {"success": True, "family_id": None, "changed": True, "path": str(path)}
        return {"success": True, "family_id": None, "changed": False, "path": str(path)}
    except OSError:
        return {"success": False, "family_id": slug, "changed": False, "path": str(path)}


def seed_family_id(workspace_path: str | Path) -> dict:
    """Alias for :func:`sync_family_id` (seed/refresh the primary family marker)."""
    return sync_family_id(workspace_path)


def family_root(slug: str) -> Path | None:
    """Nearest common ancestor directory of all family members (None if no members).

    Used as the scan root for the family code graph so backend↔frontend edges
    across correlated member projects resolve in one graph.
    """
    members = family_members(slug)
    if not members:
        return None
    parts = [list(Path(m).absolute().parts) for m in members]
    common: list[str] = []
    for idx, part in enumerate(parts[0]):
        if all(idx < len(p) and p[idx] == part for p in parts):
            common.append(part)
        else:
            break
    if not common:
        return None
    return Path(*common)


def family_db_path(slug: str) -> str:
    """Dedicated family memory store: ``~/.awlab-id/agent-memory/memory/family_{slug}.db``."""
    return str(settings.config_home / "memory" / f"family_{slug}.db")


def store_target(store: str) -> tuple[bool, str | None]:
    """Map a ``store`` param to ``(patterns: bool, family_slug: str | None)``.

    ``project`` → project memory; ``patterns`` → user-patterns store;
    ``family_<slug>`` → dedicated family store.
    """
    if store == "patterns":
        return True, None
    if store.startswith("family_"):
        return False, store[len("family_") :]
    return False, None


def create_bridge(
    workspace_path: str | Path | None = None,
    project_id: str | None = None,
    *,
    patterns: bool = False,
    family: str | None = None,
    db_path: str | Path | None = None,
) -> MCPBridge:
    """
    Create an MCPBridge instance for a project, pattern store, or family store.

    Args:
        workspace_path: The project root path.
        project_id: Optional explicit project identifier. When provided, uses
                    a dedicated per-project database file with no scope chain
                    (backward-compatible with ``agent_recall_bridge``).
                    When None, uses scope isolation within a shared database.
        patterns: When True, routes to the dedicated user-patterns store
                  (``user_patterns_db_path()``) with a global scope — independent
                  of any project database.
        family: Family slug — routes to the dedicated family store
                (``family_db_path(slug)``) so correlated projects share context.
        db_path: Explicit database path override (lowest-level escape hatch).

    Returns:
        A configured MCPBridge instance ready for use.
    """
    _patch_sqlite_for_crsqlite()

    if db_path is not None or patterns or family:
        # Dedicated store (user-patterns / family / explicit): global scope, no chain.
        if family:
            target = family_db_path(family)
        elif db_path is not None:
            target = str(db_path)
        else:
            target = user_patterns_db_path()
        _enable_wal_mode(target)
        return MCPBridge(
            db_path=target,
            default_scope="global",
            scope_chain=None,
            strict_scopes=False,
            scope_reads=True,
        )

    db_path = resolve_db_path(workspace_path=workspace_path, project_id=project_id)

    _enable_wal_mode(db_path)

    if project_id is not None:
        # Strategy A: per-project DB file (no scope isolation)
        return MCPBridge(
            db_path=db_path,
            default_scope="global",
            scope_chain=None,
            strict_scopes=False,
            scope_reads=True,
        )

    # Strategy B: single DB with scope isolation
    wp = workspace_path if workspace_path is not None else Path(".")
    scope = _get_scope(workspace_path=wp, project_id=project_id)
    config = MemoryConfig(yaml_path) if (yaml_path := settings.memory_yaml_path(wp)) else None
    return MCPBridge(
        db_path=db_path,
        default_scope=scope,
        scope_chain=["global", scope],
        config=config,
    )


# ── Convenience wrappers ─────────────────────────────────────────────────────


def search_nodes(
    workspace_path: str | Path,
    project_id: str | None = None,
    *,
    query: str,
    limit: int = 10,
    patterns: bool = False,
    family: str | None = None,
) -> list[dict]:
    """Search entities by name or observation text. Returns list of entity dicts."""
    with create_bridge(
        workspace_path=workspace_path, project_id=project_id, patterns=patterns, family=family
    ) as bridge:
        return bridge.search_nodes(query=query, limit=limit)


def open_nodes(
    workspace_path: str | Path,
    project_id: str | None = None,
    *,
    names: list[str],
    patterns: bool = False,
    family: str | None = None,
) -> list[dict]:
    """Retrieve full details for specific entities by name."""
    with create_bridge(
        workspace_path=workspace_path, project_id=project_id, patterns=patterns, family=family
    ) as bridge:
        return bridge.open_nodes(names=names)


def read_graph(
    workspace_path: str | Path,
    project_id: str | None = None,
    *,
    limit: int = 1000,
    patterns: bool = False,
    family: str | None = None,
) -> dict:
    """Read the full knowledge graph as {entities: [...], relations: [...]}."""
    with create_bridge(
        workspace_path=workspace_path, project_id=project_id, patterns=patterns, family=family
    ) as bridge:
        return bridge.read_graph(limit=limit)


def create_entities(
    workspace_path: str | Path,
    project_id: str | None = None,
    *,
    entities: list[dict],
    patterns: bool = False,
    family: str | None = None,
) -> dict:
    """Create new entities with observations. Returns {created, updated, blocked}."""
    with create_bridge(
        workspace_path=workspace_path, project_id=project_id, patterns=patterns, family=family
    ) as bridge:
        return bridge.create_entities(entities=entities)


def ensure_entities(
    workspace_path: str | Path,
    project_id: str | None = None,
    *,
    names: list[str],
    entity_type: str = "concept",
    patterns: bool = False,
    family: str | None = None,
) -> dict:
    """Create entities by name ONLY if no same-named entity exists (any type).

    This prevents the empty-duplicate side effect: when a referenced name already
    exists with a DIFFERENT entityType (e.g. ``Bus Service :: feature`` exists and
    an observation references ``Bus Service``), we REUSE the existing entity
    instead of spawning a new ``(name, concept)`` row. Returns
    ``{created: int, reused: [names], blocked: [...]}``.
    """
    names = [n for n in dict.fromkeys(names) if n]
    if not names:
        return {"created": 0, "reused": [], "blocked": []}
    with create_bridge(
        workspace_path=workspace_path, project_id=project_id, patterns=patterns, family=family
    ) as bridge:
        existing: set[str] = set()
        for i in range(0, len(names), 50):
            for e in bridge.open_nodes(names=names[i : i + 50]):
                existing.add(str(e.get("name") or ""))
        reused = [n for n in names if n in existing]
        to_create = [{"name": n, "entityType": entity_type, "observations": []} for n in names if n not in existing]
        created = (
            bridge.create_entities(entities=to_create) if to_create else {"created": 0, "updated": 0, "blocked": []}
        )
        return {"created": created, "reused": reused, "blocked": created.get("blocked", [])}


def add_observations(
    workspace_path: str | Path,
    project_id: str | None = None,
    *,
    observations: list[dict],
    patterns: bool = False,
    family: str | None = None,
    created_at: str | None = None,
) -> dict:
    """Add observations to existing entities. Returns {added, blocked}.

    ``created_at`` (G12): optional ISO-8601 timestamp. When provided, it's
    stamped onto every observation dict (``obs["created_at"] = created_at``) so
    downstream consumers (bake pipeline, retrospective, log scrapers) can
    reason about timeline ordering. The agent-recall library itself ignores
    extra fields — it uses its own internal timestamp for storage — but
    the field rides along in the dict and is readable by callers.
    Defaults to ``datetime.now(timezone.utc).isoformat()`` so callers
    that don't pass it still get a sensible value.
    """
    if created_at is None:
        created_at = datetime.now(timezone.utc).isoformat()
    stamped = []
    for obs in observations:
        stamped.append({**obs, "created_at": created_at})
    with create_bridge(
        workspace_path=workspace_path, project_id=project_id, patterns=patterns, family=family
    ) as bridge:
        return bridge.add_observations(observations=stamped)


def create_relations(
    workspace_path: str | Path,
    project_id: str | None = None,
    *,
    relations: list[dict],
    patterns: bool = False,
    family: str | None = None,
) -> dict:
    """Create directed relations between entities. Returns {created, blocked}."""
    with create_bridge(
        workspace_path=workspace_path, project_id=project_id, patterns=patterns, family=family
    ) as bridge:
        return bridge.create_relations(relations=relations)


def dedupe_entities(
    workspace_path: str | Path,
    project_id: str | None = None,
    *,
    name: str = "",
    dry_run: bool = True,
    patterns: bool = False,
    family: str | None = None,
) -> dict:
    """Merge same-named entities: keep the data-bearing one, archive the rest.

    For every name with multiple entities (optionally only ``name``), picks the
    keeper (most observations; tie-break prefers a non-``concept`` type), moves
    the others' observations into the keeper, and archives the duplicates.
    ``dry_run=True`` (default) returns the plan without mutating. This is the
    safe cleanup companion to :func:`ensure_entities` (which now prevents new
    empty duplicates from being created). Returns
    ``{success, dry_run, groups, moved_observations, archived}``.
    """
    with create_bridge(
        workspace_path=workspace_path, project_id=project_id, patterns=patterns, family=family
    ) as bridge:
        graph = bridge.read_graph(limit=1000)
        by_name: dict[str, list[dict]] = {}
        for e in graph.get("entities", []):
            by_name.setdefault(e.get("name") or "", []).append(e)

        groups: list[dict] = []
        moved_observations = 0
        archived = 0
        for n, ents in by_name.items():
            if len(ents) < 2 or (name and n != name):
                continue

            def _key(e: dict) -> tuple:
                etype = e.get("entityType") or e.get("type") or ""
                return (len(e.get("observations") or []), 0 if etype != "concept" else 1, etype)

            ordered = sorted(ents, key=_key, reverse=True)
            keeper, dupes = ordered[0], ordered[1:]
            groups.append(
                {
                    "name": n,
                    "keeper": {
                        "entityType": keeper.get("entityType") or keeper.get("type"),
                        "observations": len(keeper.get("observations") or []),
                    },
                    "duplicates": [
                        {
                            "entityType": d.get("entityType") or d.get("type"),
                            "observations": len(d.get("observations") or []),
                        }
                        for d in dupes
                    ],
                }
            )
            if dry_run:
                continue

            store = getattr(bridge, "_store", None)
            keeper_type = keeper.get("entityType") or keeper.get("type")
            keeper_id = store.find_entity(n, keeper_type) if store is not None else None
            scope = getattr(bridge, "_scope", "global")
            for d in dupes:
                d_type = d.get("entityType") or d.get("type")
                d_id = store.find_entity(n, d_type) if store is not None else None
                if d_id is None:
                    continue
                if hasattr(bridge, "_entity_writable"):
                    ok, _ = bridge._entity_writable(d_id)
                    if not ok:
                        continue
                if store is not None and keeper_id is not None and hasattr(store, "add_observation"):
                    for ob in d.get("observations") or []:
                        store.add_observation(keeper_id, ob, scope=scope)
                        moved_observations += 1
                if store is not None and hasattr(store, "delete_entity"):
                    store.delete_entity(d_id)
                    archived += 1
        return {
            "success": True,
            "dry_run": bool(dry_run),
            "groups": groups,
            "moved_observations": moved_observations,
            "archived": archived,
        }


def delete_entities(
    workspace_path: str | Path,
    project_id: str | None = None,
    *,
    names: list[str] | None = None,
    entities: list[dict] | None = None,
    patterns: bool = False,
    family: str | None = None,
) -> dict:
    """Delete entities by name (strings) and/or precise ``{name, entityType}`` specs.

    SAFETY: a bare ``name`` that matches MULTIPLE entities (same name, different
    ``entityType``) is REFUSED with a candidate list — never guess which one to
    archive (the package's name-only delete hits the first row, which can be the
    data-bearing entity). Use ``entities=[{"name", "entityType"}]`` to delete a
    specific one. Returns {deleted, blocked}.
    """
    names = list(names or [])
    specs = list(entities or [])
    result: dict = {"deleted": 0, "blocked": []}

    with create_bridge(
        workspace_path=workspace_path, project_id=project_id, patterns=patterns, family=family
    ) as bridge:

        def _named(name: str) -> list[dict]:
            graph = bridge.read_graph(limit=1000)
            return [
                {
                    "entityType": e.get("entityType") or e.get("type") or "unknown",
                    "observation_count": len(e.get("observations") or []),
                }
                for e in graph.get("entities", [])
                if (e.get("name") or "") == name
            ]

        for name in dict.fromkeys(names):
            matches = _named(name)
            if not matches:
                result["blocked"].append(f"Entity not found: '{name}'")
            elif len(matches) > 1:
                cands = ", ".join(f"{m['entityType']} ({m['observation_count']} obs)" for m in matches)
                result["blocked"].append(
                    f"Ambiguous name '{name}' — {len(matches)} entities ({cands}). "
                    f"Pass entities=[{{'name': '{name}', 'entityType': ...}}] to pick one."
                )
            else:
                r = bridge.delete_entities(names=[name])
                result["deleted"] += r.get("deleted", 0)
                result["blocked"] += r.get("blocked", [])

        store = getattr(bridge, "_store", None)
        for spec in specs:
            name, etype = spec.get("name"), spec.get("entityType")
            if store is None or not (hasattr(store, "find_entity") and hasattr(store, "delete_entity")):
                result["blocked"].append(f"Cannot delete '{name}' :: {etype} by type (store API unavailable)")
                continue
            eid = store.find_entity(name, etype)
            if eid is None:
                result["blocked"].append(f"Entity not found: '{name}' :: {etype}")
                continue
            if hasattr(bridge, "_entity_writable"):
                ok, reason = bridge._entity_writable(eid)
                if not ok:
                    result["blocked"].append(reason)
                    continue
            store.delete_entity(eid)
            result["deleted"] += 1
    return result


def delete_relations(
    workspace_path: str | Path,
    project_id: str | None = None,
    *,
    relations: list[dict],
    patterns: bool = False,
    family: str | None = None,
) -> dict:
    """Archive relations. Returns {deleted, blocked}."""
    with create_bridge(
        workspace_path=workspace_path, project_id=project_id, patterns=patterns, family=family
    ) as bridge:
        return bridge.delete_relations(relations=relations)


def delete_observations(
    workspace_path: str | Path,
    project_id: str | None = None,
    *,
    deletions: list[dict],
    patterns: bool = False,
    family: str | None = None,
) -> dict:
    """Archive observations by text match. Returns {deleted, blocked}."""
    with create_bridge(
        workspace_path=workspace_path, project_id=project_id, patterns=patterns, family=family
    ) as bridge:
        return bridge.delete_observations(deletions=deletions)
