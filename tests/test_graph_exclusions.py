"""
Unified exclusion engine — .gitignore + .graphignore combined, gitignore-style
glob matching for FILES AND DIRECTORIES alike.

Locks in the fixes from plan 183eba8c Phase 2.1:
1. A bare ``*``/``**`` (ignore-all) pattern — common in Laravel-style nested
   .gitignore files (``*`` + ``!.gitignore``) — must NOT become a global
   basename glob that excludes every file (the eka-panel stall root cause).
2. ``.gitignore`` and ``.graphignore`` merge into ONE additive rule set with
   identical semantics.
3. Globs match directories too: ``dist-*/`` prunes dirs, ``**/cache/`` prunes
   nested caches, and an exact dir path (``public/build``) excludes the subtree.
4. A blank-detection guard keeps exclusions from ever emptying the source set.
"""

import shutil
from pathlib import Path

from mcp_server.helpers import graphify_bridge as gb


def _mk(root: Path, files: dict[str, str]) -> None:
    if root.exists():
        shutil.rmtree(root)
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def _manifest(root: Path) -> set[str]:
    ex = gb._gitignore_exclusions(root)
    return set(gb._source_manifest(root, ex))


# ── Bare '*' / '**' must never empty the source set (eka-panel root cause) ─


def test_bare_star_gitignore_does_not_empty_manifest(tmp_path: Path):
    """Laravel-style `*` + `!` .gitignore (storage/app) must not exclude every file."""
    _mk(
        tmp_path,
        {
            ".gitignore": "*\n!.gitignore\n",
            "src/app.py": "def app():\n    return 1\n",
            "storage/app/.gitignore": "*\n!.gitignore\n!private/\n",
            "storage/app/junk.log": "x",
            "storage/app/keep.py": "def keep():\n    return 1\n",
        },
    )
    m = _manifest(tmp_path)
    assert "src/app.py" in m  # the bare '*' no longer empties the manifest
    # The parser never registers a global '*' basename glob.
    ex = gb._gitignore_exclusions(tmp_path)
    assert "*" not in ex.name_globs
    assert "*" not in ex.dir_names


def test_bare_star_in_graphignore_skipped(tmp_path: Path):
    """Same skip applies to .graphignore."""
    _mk(tmp_path, {".graphignore": "*\n", "a.py": "def a():\n    return 1\n"})
    assert "a.py" in _manifest(tmp_path)


# ── Combined .gitignore + .graphignore, same file/dir glob style ───────────


def test_graphignore_and_gitignore_combine(tmp_path: Path):
    """A file excluded by either file is excluded from the graph."""
    _mk(
        tmp_path,
        {
            ".gitignore": "via_git.py\n",
            ".graphignore": "via_graph.py\ngenerated/\n",
            "via_git.py": "def vg():\n    return 1\n",
            "via_graph.py": "def vx():\n    return 1\n",
            "generated/gen.py": "def gen():\n    return 1\n",
            "keep.py": "def keep():\n    return 1\n",
        },
    )
    m = _manifest(tmp_path)
    assert "via_git.py" not in m
    assert "via_graph.py" not in m
    assert "generated/gen.py" not in m
    assert "keep.py" in m


def test_dir_basename_glob_prunes_directories(tmp_path: Path):
    """`dist-*/` prunes any directory whose name matches the glob (files + nested)."""
    _mk(
        tmp_path,
        {
            ".graphignore": "dist-*/\n",
            "dist-a/x.js": "export const x = 1\n",
            "dist-b/nested/y.js": "export const y = 1\n",
            "src/main.js": "export const m = 1\n",
            "distiller/keep.js": "export const k = 1\n",  # 'distiller' does not match 'dist-*'
        },
    )
    m = _manifest(tmp_path)
    assert "dist-a/x.js" not in m
    assert "dist-b/nested/y.js" not in m
    assert "distiller/keep.js" in m
    assert "src/main.js" in m


def test_dir_path_glob_prunes_nested_caches(tmp_path: Path):
    """`**/cache/` prunes cache directories at any depth."""
    _mk(
        tmp_path,
        {
            ".graphignore": "**/cache/\n",
            "src/cache/c.js": "export const c = 1\n",
            "src/deep/level/cache/c2.js": "export const c2 = 1\n",
            "src/keep/k.js": "export const k = 1\n",
        },
    )
    m = _manifest(tmp_path)
    assert "src/cache/c.js" not in m
    assert "src/deep/level/cache/c2.js" not in m
    assert "src/keep/k.js" in m


def test_exact_dir_path_excludes_subtree(tmp_path: Path):
    """`public/build` (no trailing slash) excludes the dir and everything under it."""
    _mk(
        tmp_path,
        {
            ".graphignore": "public/build\n",
            "public/build/b.js": "export const b = 1\n",
            "public/ok.js": "export const o = 1\n",
        },
    )
    m = _manifest(tmp_path)
    assert "public/build/b.js" not in m
    assert "public/ok.js" in m


def test_file_basename_glob_any_level(tmp_path: Path):
    """`*.min.js` excludes minified files at any depth."""
    _mk(
        tmp_path,
        {
            ".gitignore": "*.min.js\n",
            "a.min.js": "var a = 1\n",
            "public/vendor/lib.min.js": "var b = 1\n",
            "src/main.js": "export const m = 1\n",
        },
    )
    m = _manifest(tmp_path)
    assert "a.min.js" not in m
    assert "public/vendor/lib.min.js" not in m
    assert "src/main.js" in m


def test_glob_matches_both_file_and_dir_names(tmp_path: Path):
    """A non-dir-only glob (`*.bak`) matches a FILE or a DIRECTORY of that name."""
    _mk(
        tmp_path,
        {
            ".graphignore": "*.bak\n",
            "old.bak": "x",
            "backup.bak/inside.js": "export const i = 1\n",
            "keep.txt": "y",
        },
    )
    m = _manifest(tmp_path)
    assert "old.bak" not in m
    assert "backup.bak/inside.js" not in m  # dir pruned by the same basename glob
    assert "keep.txt" in m


# ── Blank-detection guard ──────────────────────────────────────────────────


def test_exclusions_never_empty_manifest(tmp_path: Path):
    """If exclusion rules would empty the set, fall back to _NOISE_DIRS-only."""
    _mk(
        tmp_path,
        {
            ".graphignore": "*.*\n",  # would ignore every file (incl. the ignore file)
            "a.py": "def a():\n    return 1\n",
        },
    )
    m = _manifest(tmp_path)
    assert "a.py" in m  # guard restored it (matches _build_graph_impl's files guard)


def test_gitignored_matches_dir_glob_subtree(tmp_path: Path):
    """_gitignored() flags a file inside a glob-excluded dir."""
    _mk(
        tmp_path,
        {
            ".graphignore": "build-*/\n",
            "build-x/bundle.js": "x",
            "src/a.js": "y",
        },
    )
    ex = gb._gitignore_exclusions(tmp_path)
    assert gb._gitignored(tmp_path, tmp_path / "build-x" / "bundle.js", ex) is True
    assert gb._gitignored(tmp_path, tmp_path / "src" / "a.js", ex) is False
