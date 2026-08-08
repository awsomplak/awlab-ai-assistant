#!/usr/bin/env python3
"""
Lint & code-hygiene runner for AWLab-ID.

Easy to call from scripts/::

    python scripts/lint.py            # lint + format check
    python scripts/lint.py --fix      # auto-fix lint violations, then format check
    python scripts/lint.py --format   # also APPLY ruff format (not just check)
    python scripts/lint.py src/tests  # lint specific paths

Checks (via ruff — declared in pyproject.toml [dev]):
  - lint rules (E, F, I, W): unused imports, import sorting, structure, newline-at-EOF
  - formatting (ruff format --check)

Import hygiene rule (enforced by ruff E402 + review):
  - Module-level imports MUST be at the top of the file, before any code.
  - In-method lazy imports are ALLOWED only for optional/heavy dependencies
    (e.g. ``fastembed``) and must carry a short comment:  ``# lazy: optional dep``
  - Do NOT add blanket per-file ``noqa`` ignores for sloppy imports.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TARGETS = ["src", "tests", "scripts"]


def _ruff_cmd() -> list[str]:
    """Prefer the ruff binary next to the venv python, else ``python -m ruff``."""
    scripts_dir = Path(sys.executable).parent
    for name in ("ruff.exe", "ruff"):
        candidate = scripts_dir / name
        if candidate.exists():
            return [str(candidate)]
    return [sys.executable, "-m", "ruff"]


def _vulture_cmd() -> list[str]:
    """Prefer the vulture binary next to the venv python, else ``python -m vulture``."""
    scripts_dir = Path(sys.executable).parent
    for name in ("vulture.exe", "vulture"):
        candidate = scripts_dir / name
        if candidate.exists():
            return [str(candidate)]
    return [sys.executable, "-m", "vulture"]


def _run(label: str, cmd: list[str]) -> int:
    print(f"\n==> {label}\n    {subprocess.list2cmdline(cmd)}")
    proc = subprocess.run(cmd, cwd=ROOT)
    return proc.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Lint & code-hygiene runner (ruff).")
    parser.add_argument("--fix", action="store_true", help="Auto-fix lint violations")
    parser.add_argument(
        "--format",
        dest="apply_format",
        action="store_true",
        help="APPLY ruff format (default is --check only)",
    )
    parser.add_argument(
        "--deadcode",
        action="store_true",
        help="Also run a vulture dead-code scan (repeatable audit: src + .vulture_whitelist.py)",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        default=DEFAULT_TARGETS,
        help=f"Paths to lint (default: {' '.join(DEFAULT_TARGETS)})",
    )
    args = parser.parse_args()

    ruff = _ruff_cmd()

    check_cmd = [*ruff, "check", *args.paths]
    if args.fix:
        check_cmd.append("--fix")

    if args.apply_format:
        format_cmd = [*ruff, "format", *args.paths]
    else:
        format_cmd = [*ruff, "format", "--check", *args.paths]

    code = 0
    code |= _run("Lint (ruff check)", check_cmd)
    code |= _run("Format (ruff format)", format_cmd)

    if args.deadcode:
        code |= _run(
            "Dead code (vulture)",
            [
                *_vulture_cmd(),
                "src",
                str(ROOT / ".vulture_whitelist.py"),
                "--min-confidence",
                "60",
                "--ignore-names",
                "__doc__",
            ],
        )

    if code == 0:
        print("\n[OK] Lint & format clean.")
    else:
        print("\n[FAIL] Issues found. Run: python scripts/lint.py --fix  (then review the changes).")
    return code


if __name__ == "__main__":
    sys.exit(main())
