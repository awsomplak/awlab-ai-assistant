# ruff: noqa: E501
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# ── Vite/JS path-alias import augmentation ─────────────────────────────────
#
# graphifyy resolves relative imports and tsconfig/jsconfig ``paths`` aliases,
# but NOT Vite's ``resolve.alias`` (a common Vue/Nuxt/Vite convention where no
# tsconfig exists — e.g. ``'@': './src'`` in vite.config.js). Unresolved alias
# specifiers like ``import { useAuthStore } from '@/stores/auth'`` silently
# produce NO edge, which breaks ``graph_path``/module navigation for .vue SFCs
# and any ``@/``-importing file. This post-build pass (a layer like
# ``_gitignore_exclusions``) teaches the graph about those aliases so the
# missing ``imports_from``/``imports`` edges are added.

_VITE_CONFIG_FILES = ("vite.config.js", "vite.config.mjs", "vite.config.cjs", "vite.config.ts", "vite.config.mts")
_NUXT_CONFIG_FILES = ("nuxt.config.js", "nuxt.config.mjs", "nuxt.config.ts")
_ALIAS_CONFIG_FILES = _VITE_CONFIG_FILES + _NUXT_CONFIG_FILES


# Extension candidates when resolving an extensionless alias specifier
# (``'@/stores/auth'`` → ``stores/auth.js`` / ``stores/auth/index.ts`` ...).
_ALIAS_RESOLVE_EXTS = (".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs", ".vue", ".svelte", ".astro")


def _resolve_alias_replacement(repl: str, base: Path) -> Path | None:
    """Turn a Vite alias replacement expression into an absolute path.

    Handles the common forms: a plain relative string (``'./src'``), an
    absolute string (``'D:/x/src'`` / ``'/abs'``), and the ubiquitous
    ``fileURLToPath(new URL('./src', import.meta.url))`` wrapper (the URL
    string is extracted and resolved against the config's directory).
    """
    repl = repl.strip()
    url = re.search(r"""new\s+URL\(\s*['"]([^'"]+)['"]""", repl)
    if url:
        repl = url.group(1).strip()
    p = Path(repl)
    if p.is_absolute():
        return p.resolve()
    rel = repl.strip("'\" `,")
    if not rel:
        return None
    return (base / rel).resolve()


def _vite_alias_map(scan_root: Path) -> dict[str, Path]:
    """Discover path aliases (``'@': './src'`` etc.) from Vite/Nuxt config.

    graphifyy reads tsconfig/jsconfig ``paths`` but never Vite's
    ``resolve.alias`` — the config is searched from ``scan_root`` upward
    (≤3 parents) so ``root="src"`` scans still find the project's config.
    Returns {alias key: absolute target dir}, or {} when none is found.
    """
    for base in (scan_root, *scan_root.parents[:3]):
        for name in _ALIAS_CONFIG_FILES:
            cfg = base / name
            if not cfg.is_file():
                continue
            try:
                text = cfg.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            aliases: dict[str, Path] = {}
            # Object form: alias: { '@': './src', '@components': '...' }  (string or URL)
            for m in re.finditer(r"""['"]([@~][^'"]*)['"]\s*:\s*['"]([^'"]+)['"]""", text):
                target = _resolve_alias_replacement(m.group(2), base)
                if target is not None:
                    aliases[m.group(1)] = target
            for m in re.finditer(
                r"""['"]([@~][^'"]*)['"]\s*:\s*fileURLToPath\(\s*new\s+URL\(\s*['"]([^'"]+)['"]""",
                text,
            ):
                target = _resolve_alias_replacement(m.group(2), base)
                if target is not None:
                    aliases[m.group(1)] = target
            # Array form: alias: [ { find: '@', replacement: './src' }, ... ]
            for m in re.finditer(r"""find\s*:\s*['"]([@~][^'"]*)['"]\s*,\s*replacement\s*:\s*['"]([^'"]+)['"]""", text):
                target = _resolve_alias_replacement(m.group(2), base)
                if target is not None:
                    aliases[m.group(1)] = target
            for m in re.finditer(
                r"""find\s*:\s*['"]([@~][^'"]*)['"]\s*,\s*replacement\s*:\s*fileURLToPath\(\s*new\s+URL\(\s*['"]([^'"]+)['"]""",
                text,
            ):
                target = _resolve_alias_replacement(m.group(2), base)
                if target is not None:
                    aliases[m.group(1)] = target
            if aliases:
                return aliases
    return {}


# Import/export statement matcher: named/bare/dynamic forms, single or double
# quotes, multi-line. Group ``spec`` = from-form specifier, ``bare``/``dyn`` =
# side-effect / dynamic-import forms, ``clause`` = the imported-name clause.
# NOTE: kept on ONE line — literal whitespace inside a multi-line pattern is
# matched literally, which silently breaks the regex.
_ALIAS_IMPORT_RE = re.compile(
    r"""\b(?:import|export)\b(?:\s+(?P<clause>\{[^{}]*\}|[^;"']*?)\s+from\s+['"](?P<spec>[^"']+)['"]|\s+['"](?P<bare>[^"']+)['"]|\s*\(\s*['"](?P<dyn>[^"']+)['"]\s*\))""",
    re.M | re.S,
)
_ALIAS_REQUIRE_RE = re.compile(r"""\brequire\s*\(\s*['"]([^"']+)['"]\s*\)""", re.M | re.S)


def _imported_names(clause: str | None) -> list[str]:
    """Extract imported symbol names from an import clause.

    ``{ useAuthStore, type T }`` → [useAuthStore]; ``api`` → [api];
    ``* as ns`` → []; ``''`` → [].
    """
    clause = (clause or "").strip()
    if not clause:
        return []
    if clause.startswith("{"):
        names: list[str] = []
        for part in clause.strip("{} ").split(","):
            part = part.strip()
            if not part or part == "type":
                continue
            if part.startswith("type "):
                part = part[len("type ") :].strip()
            base = part.split(" as ")[0].strip()
            if base and base != "type":
                names.append(base)
        return names
    if clause.startswith("*"):
        return []  # namespace import — no named symbol edge
    base = clause.split(" as ")[0].strip()
    return [base] if base else []


def _alias_import_specifiers(text: str, aliases: dict[str, Path]):
    """Yield (specifier, imported_names, line) for alias imports in ``text``.

    Only specifiers that start with a declared alias prefix (e.g. ``@/...``)
    are yielded — relative and bare-package imports are graphifyy's domain.
    """
    keys = sorted(aliases, key=len, reverse=True)

    def _is_alias(spec: str) -> bool:
        return any(spec == k or spec.startswith(k + "/") for k in keys)

    for m in _ALIAS_IMPORT_RE.finditer(text):
        spec = m.group("spec") or m.group("bare") or m.group("dyn")
        if not spec or not _is_alias(spec):
            continue
        line = text.count("\n", 0, m.start()) + 1
        yield spec, _imported_names(m.group("clause")), line
    for m in _ALIAS_REQUIRE_RE.finditer(text):
        spec = m.group(1)
        if not spec or not _is_alias(spec):
            continue
        line = text.count("\n", 0, m.start()) + 1
        yield spec, [], line


def _resolve_alias_abs(spec: str, aliases: dict[str, Path]) -> Path | None:
    """Resolve an alias specifier to an absolute path, or None if not aliased."""
    for key in sorted(aliases, key=len, reverse=True):
        if spec == key or spec.startswith(key + "/"):
            return aliases[key] / spec[len(key) :].lstrip("/")
    return None


def _match_module_abs(modules: dict[str, str], cand: Path) -> tuple[str, str] | None:
    """Find an existing module node for a resolved alias path.

    Tries the exact path, then extension and ``/index.*`` candidates for
    extensionless specifiers, then a case-insensitive fallback (Windows FS).
    Returns ``(node_id, source_key)`` — ``source_key`` is the exact module
    source path that matched, needed to look up sibling symbol nodes.
    """
    key = str(cand).replace("\\", "/")
    candidates = [key]
    if not Path(cand).suffix:
        for ext in _ALIAS_RESOLVE_EXTS:
            candidates.append(key + ext)
            candidates.append(key + "/index" + ext)
    for c in candidates:
        hit = modules.get(c)
        if hit is not None:
            return hit, c
    lowered = {k.lower(): v for k, v in modules.items()}
    for c in candidates:
        hit = lowered.get(c.lower())
        if hit is not None:
            return hit, c
    return None


def _mk_alias_link(source: str, target: str, relation: str, src: str, line: int) -> dict[str, Any]:
    """A graphify-style link for an alias-resolved import edge."""
    return {
        "source": source,
        "target": target,
        "relation": relation,
        "context": "import",
        "confidence": "EXTRACTED",
        "source_file": src,
        "source_location": f"L{line}",
        "weight": 1.0,
        "alias_resolved": True,
    }


def _compute_alias_edges(
    node_items: list[tuple[str, str, str]],
    scan_root: Path,
    aliases: dict[str, Path],
) -> list[dict[str, Any]]:
    """Compute missing alias-import links from graph nodes + a source re-scan.

    ``node_items`` is an iterable of ``(node_id, source_file, label)``. Module
    nodes (label == source basename) are re-scanned for alias imports; each
    resolvable specifier yields an ``imports_from`` module edge plus ``imports``
    symbol edges for named imports that resolve to existing symbol nodes.
    """
    modules: dict[str, str] = {}  # abs source path (fwd-slash) -> module node id
    symbols: dict[tuple[str, str], str] = {}  # (abs src, name lower) -> symbol node id
    for nid, src, label in node_items:
        if not src:
            continue
        key = str((scan_root / src).resolve()).replace("\\", "/")
        modules.setdefault(key, nid)
        base = (label or "").split("(", 1)[0].strip()
        if base:
            symbols.setdefault((key, base.lower()), nid)

    links: list[dict[str, Any]] = []
    for nid, src, label in node_items:
        if not src or (label or "") != src.rsplit("/", 1)[-1]:
            continue  # only module nodes import other files
        file = scan_root / src
        try:
            text = file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for spec, names, line in _alias_import_specifiers(text, aliases):
            cand = _resolve_alias_abs(spec, aliases)
            if cand is None:
                continue
            matched = _match_module_abs(modules, cand)
            if matched is None or matched[0] == nid:
                continue
            tgt_id, tgt_key = matched
            links.append(_mk_alias_link(nid, tgt_id, "imports_from", src, line))
            for name in names:
                sym_id = symbols.get((tgt_key, name.lower()))
                if sym_id and sym_id != nid:
                    links.append(_mk_alias_link(nid, sym_id, "imports", src, line))
    return links


def _dedup_links(links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop duplicate (source, target, relation) links."""
    seen: set[tuple[str, str, str]] = set()
    out: list[dict[str, Any]] = []
    for e in links:
        key = (str(e.get("source")), str(e.get("target")), str(e.get("relation")))
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


def _augment_alias_import_edges(graph, scan_root: Path) -> int:
    """Add missing alias-import edges to the built networkx graph (in place).

    Returns the number of edges added. Idempotent: existing edges are never
    duplicated, and a project without Vite/Nuxt aliases is a no-op.
    """
    aliases = _vite_alias_map(scan_root)
    if not aliases:
        return 0
    node_items = [
        (nid, (attrs.get("source_file") or ""), (attrs.get("label") or "")) for nid, attrs in graph.nodes(data=True)
    ]
    links = _dedup_links(_compute_alias_edges(node_items, scan_root, aliases))
    added = 0
    for e in links:
        src, tgt = str(e.get("source")), str(e.get("target"))
        if graph.has_edge(src, tgt):
            continue
        attrs = {k: v for k, v in e.items() if k not in ("source", "target")}
        graph.add_edge(src, tgt, **attrs)
        added += 1
    return added


def _augment_alias_import_edges_json(graph_path: Path, scan_root: Path) -> int:
    """Self-heal a persisted graph.json with missing alias-import links.

    Used by the fresh-skip path so graphs built before this pass (or by an
    older version) gain the alias edges on their next no-op read without a
    full rebuild. Returns the number of links added; no-op without aliases.
    """
    aliases = _vite_alias_map(scan_root)
    if not aliases or not graph_path.is_file():
        return 0
    try:
        data = json.loads(graph_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    node_items = [
        (str(n.get("id") or ""), (n.get("source_file") or ""), (n.get("label") or "")) for n in data.get("nodes", [])
    ]
    links = _dedup_links(_compute_alias_edges(node_items, scan_root, aliases))
    if not links:
        return 0
    existing = {(str(e.get("source")), str(e.get("target"))) for e in data.get("links", [])}
    new = [e for e in links if (e["source"], e["target"]) not in existing]
    if not new:
        return 0
    data.setdefault("links", []).extend(new)
    graph_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(new)
