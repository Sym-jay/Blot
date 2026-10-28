# Blot: notes for Claude

Blot is a pre-commit check that has read a repo's git history. It warns (never blocks) when a commit
removes a fix (`removed-fix`), re-adds a line a fix removed (`readded-bug`), or forgets a file that
always changes with another (`forgot-related-file`). Mascot: Sora the slime. Owner: Shyam (GitHub
`Sym-jay`). PyPI name `blot-git`, command `blot`. Repo: https://github.com/Sym-jay/Blot

## Layout
- `src/blot/`: stdlib-only on Python 3.11+ (3.10 also needs `tomli`; keep it that way)
  - `cli.py` commands: index, check, why, install-hook, bench. `check`/`why` build/refresh the index themselves.
  - `index.py` builds `.git/blot.db` (parallel `git log` slices for 2000+ commits); `store.py` SQLite schema v2
  - `classify.py` fix severity 0/1/2 + refactor detection: most precision lives here
  - `checks/` one file per check; `trace.py` blame that follows lines past refactor commits
  - `why.py`, `github.py` (`index --github` via `gh api graphql`), `bench.py` (cases + `--replay N`)
- `bench/cases.json` scripted real-world cases (9/9); `bench/RESULTS.md` replay numbers
- `action.yml` GitHub Action; `.github/workflows/blot.yml` runs it on Blot's own PRs
- `assets/sora/build_sprites.py` and `assets/demo/make_demo.py` regenerate the mascot and demo GIF
- `docs/PRD.md` roadmap; `docs/PYPI.md` is the PyPI page (no relative images there)

## Develop
```bash
python -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev]"
.venv/Scripts/python -m pytest -q
.venv/Scripts/blot bench --workdir <short path>     # must stay 9/9
```
- Any change to `classify.py` or a check: run `blot bench --replay 500` on Flask/Express/Django
  clones, read the warnings, add a bench case for each new noise pattern. Target ≤ 1 warning / 10 commits.
- `blot check` must stay under ~500 ms and never fail a commit because of Blot's own errors.

## Release (each release so far followed this)
1. Branch, bump `version` in `pyproject.toml` and `src/blot/__init__.py`, PR, wait for CI (6 jobs + "blot").
2. Squash-merge, then `gh release create vX.Y.Z --target main`: `.github/workflows/release.yml`
   publishes to PyPI via trusted publishing. PyPI/pip can lag a minute or two after publish.
3. Verify: fresh venv, `pip install blot-git==X.Y.Z`, run `blot why src/flask/app.py:355` on a Flask clone.

## Conventions
- Work on a branch, open a PR, merge only when the owner says so. Squash-merge.
- Commits end with `Co-Authored-By: Claude ...`; PR bodies end with the Claude Code line.
- Contributors (e.g. O1sumitkumar) should comment on the issue before starting a PR.
- Never claim results that weren't run; label replay findings honestly (e.g. the Express
  CVE-2024-51999 advisory was later withdrawn, so its revert was deliberate).

## Windows gotchas (this machine)
- Long paths: clone big repos and make venvs under a short path like `%LOCALAPPDATA%\Temp\wl\`.
- The user folder has a space ("N Jayasankar"): quote paths; don't word-split commands in loops.
- Writing code through bash heredocs mangles `\b`, `\n`, `\s`: use the Edit tool for regexes/strings.
- `gh api` paths: no leading slash (Git Bash rewrites it).

## State (2026-10-28)
- Released 0.4.0: checks, `why`, `--github`, bench/replay, parallel indexing, pre-commit framework
  support (`rev: v0.4.0`), GitHub Action (`uses: Sym-jay/Blot@v0.4.0`).
- Launch: dev.to post published 2026-10-24; the CVE sentence there should mention the withdrawn advisory.
- Open issues: #2–#6 (bench cases for Django, Express, React, requests, Flask).
- Next: LinkedIn/Show HN, gather false-alarm reports, Django/React noise (~1.3 / 10 commits).
