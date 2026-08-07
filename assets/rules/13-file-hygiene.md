<!-- → authority: 00-meta.md -->
# File & Workspace Hygiene — Scratch / Temp Files

## Purpose
Define a single, strict home for **scratch files**: files the agent creates ONLY to run a quick check, test, or throwaway execution — and that are NOT part of the project deliverable. This keeps temporary junk out of the project root and out of git history.

## The Scratch Directory (canonical + consistent)

- **Always** generate scratch files inside the designated scratch directory:
  - Primary: `<project>/.ai/temp/`
  - Fallback (no `.ai/` present): `<project>/.temp/`
- **Consistency is mandatory** — always reuse the SAME scratch directory for every throwaway file. Never scatter scratch across the project.

## Git Ignoring (directory, never individual files)

- The scratch **directory** is git-ignored (`.ai` is already ignored; `.temp/` is added to `.gitignore`).
- ❌ **Never** gitignore individual scratch files — that accumulates junk entries in `.gitignore`.
- If the chosen scratch directory is not already ignored, add ONE directory entry to `.gitignore` **before** writing any scratch file (e.g. `/.temp/` + `*/**/.temp/`).

## Hard Prohibitions

- ❌ **Never** create scratch files in the project root, `src/`, `tests/`, `docs/`, `scripts/`, `assets/`, or beside real code.
- ❌ **Never** reference scratch files in `task_update` tasks, memory entities, plans, or commit messages.
- ❌ **Never** commit a file that lives in the scratch directory. If a scratch file turns out to be a real deliverable, **move** it to its proper location (and correct path) before committing — never commit from temp.
- ❌ **Never** name a scratch file so it could be mistaken for project code (no `helper.py`, `main.py`, `utils.py` in temp).

## Cleanup

- Delete scratch files after the check completes.
- Do not keep scratch across sessions — the scratch directory is ephemeral by contract.
- The scratch directory is never scanned by `ctx_info mode="scan"` and never indexed into the code graph (`_NOISE_DIRS` excludes `.ai`; `.temp/` is excluded too).

## Decision Checklist

When about to create a new file, verify:
- [ ] Is this file part of the deliverable? → put it in the correct real path, NOT temp.
- [ ] Is this a throwaway check/execution? → put it in `.ai/temp/` (or `.temp/`), nothing else.
- [ ] Is the scratch directory git-ignored (one directory entry)? If not, add it FIRST.
- [ ] Will this file be referenced in tasks/memory/commits? → it must not be.
