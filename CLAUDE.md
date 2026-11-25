# Blot: notes for Claude

Blot is a pre-commit check that has read a repo's git history. It warns (never blocks) when a commit
removes a fix (`removed-fix`), re-adds a line a fix removed (`readded-bug`), or forgets a file that
always changes with another (`forgot-related-file`). Mascot: Sora the slime. Owner: Shyam (GitHub
`Sym-jay`). PyPI name `blot-git`, command `blot`. Repo: https://github.com/Sym-jay/Blot

## Layout
- `src/blot/`: stdlib-only on Python 3.11+ (3.10 also needs `tomli`; keep it that way)
  - `cli.py` commands: index, check, why, map, mcp, install-hook, guard, install-agent-hook, install-mcp, bench.
    `check`/`why`/`map`/`mcp` build/refresh the index themselves (`index.open_index`).
  - `index.py` builds `.git/blot.db` (parallel `git log` slices for 2000+ commits); `store.py` SQLite schema v2
  - `classify.py` fix severity 0/1/2 + refactor detection: most precision lives here
  - `checks/` one file per check; `trace.py` blame that follows lines past refactor commits
  - `fixmap.py` (`blot map`), `mcp.py` (stdio MCP server, both protocol eras; `install-mcp` in `hooks.py`)
  - `guard.py` (Claude Code hook; installer in `hooks.py`), `why.py`, `github.py` (`index --github` via `gh api graphql`), `bench.py` (cases + `--replay N`)
- `bench/cases.json` scripted real-world cases (33/33); `bench/labels/` hand verdicts for every replay
  warning on the 5 reference repos; `bench/RESULTS.md` replay numbers, precision and recall
- `bench/regressions.py` (collect / run / report) + `bench/regressions/<repo>.json`: recall on real
  regressions (culprit commits named by a later fix), with hand judgements per pair
- `action.yml` GitHub Action; `.github/workflows/blot.yml` runs it on Blot's own PRs
- `assets/sora/build_sprites.py` and `assets/demo/make_demo.py` regenerate the mascot and demo GIF
- `docs/PRD.md` roadmap; `docs/PYPI.md` is the PyPI page (no relative images there)

## Develop
```bash
python -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev]"
.venv/Scripts/python -m pytest -q
.venv/Scripts/blot bench --workdir <short path>     # must stay 33/33 (e.g. %LOCALAPPDATA%\Temp\wl\b33)
```
- Any change to `classify.py` or a check: on each of the 5 clones, checked out at the `head` in its
  labels file, run `blot bench --replay 500 --labels bench/labels/<repo>.json --update-labels`. Keep a
  rule only if precision rises and no `useful` warning disappears; label any new `null` verdicts
  (rules in bench/README.md). Targets: ≤ 1 warning / 10 commits, precision ≥ 80%.
- Clones live in `%LOCALAPPDATA%\Temp\wl\rp\` (labelled heads; `...\wl\replay\` was wiped by a temp
  cleanup on 2026-11-16) and `...\wl\b33\` (case runs; `wl\bench\` was emptied too, so b33 was cloned from
  `rp\` with `origin` set to GitHub). Temp can be cleared: re-clone if `.git` is empty.
- `blot check` must stay under ~500 ms and never fail a commit because of Blot's own errors.

## Release (each release so far followed this)
1. Branch, bump `version` in `pyproject.toml` and `src/blot/__init__.py`, PR, wait for CI (6 jobs + "blot").
2. Squash-merge, then `gh release create vX.Y.Z --target main`: `.github/workflows/release.yml`
   publishes to PyPI via trusted publishing. PyPI/pip can lag a minute or two after publish.
3. Verify: fresh venv, `pip install blot-git==X.Y.Z`, run `blot why src/flask/app.py:355` on a Flask clone.

## Conventions
- Work on a branch, open a PR, merge only when the owner says so. Squash-merge.
- No `Co-Authored-By` trailer in commits or PRs (the owner's choice); PR bodies end with the Claude Code line.
- Contributors (e.g. O1sumitkumar) should comment on the issue before starting a PR.
- Never claim results that weren't run; label replay findings honestly (e.g. the Express
  CVE-2024-51999 advisory was later withdrawn, so its revert was deliberate).

## Windows gotchas (this machine)
- Long paths: clone big repos and make venvs under a short path like `%LOCALAPPDATA%\Temp\wl\`.
- The user folder has a space ("N Jayasankar"): quote paths; don't word-split commands in loops.
- Writing code through bash heredocs mangles `\b`, `\n`, `\s`: use the Edit tool for regexes/strings.
- `gh api` paths: no leading slash (Git Bash rewrites it).

## State (2026-11-21)
- Released 0.7.1: checks, `why`, `map`, `--github`, agent guard (`blot guard` / `install-agent-hook`),
  MCP server (`blot mcp` / `install-mcp`, tools `explain_lines` and `load_bearing_lines`), bench/replay
  with labels, parallel indexing, pre-commit framework (`rev: v0.7.1`), GitHub Action (`@v0.7.1`).
  0.7.1 adds the docstring rule (#40) and the structural `KeptLines` matcher (#45).
- Live-tested in Claude Code: the guard (2026-11-06: the agent cited fix 8efea0c it was undoing) and MCP
  (2026-11-14: an agent called `load_bearing_lines` on its own and answered correctly).
  Throwaway test folder: `Documents\flask-guard-test` (guard hook + `.mcp.json`).
- `blot map` takes ~1.5 s on Flask app.py. `trace_lines(whole_file=True)` drops `-C` from the first blame so
  map and why agree; checks still use the old path.
- Recall (2026-11-25, first measurement, 0.7.1 checks): 137 culprit/fix pairs (newest 40 per repo for
  Django/React), 71 real bugs; 11 warned on target = **15% raw, 11/25 (44%) within Blot's model**.
  Django 10/39, Express 1/8, Flask/requests/React 0. All 6 in-model misses are classification gaps
  (fixes without fix words: "loosen ...", "Make sure ...", "Closes #632", Django `Fixed #N -- Changed`
  at severity 1); none came from tracing or the matcher. 46 misses are outside the model (new code,
  feature lines, build/deps). Re-run with `bench/regressions.py run` after any classify/check change.
  Judging rule: a revert is a bug only if a message/PR/changelog/ticket says what broke.
- Precision (2026-11-21, after #45): 69/124 useful (55.6%); Django 1.43 / 10 commits (over the ≤ 1
  target), others ≤ 0.62.
  `forgot-related-file` has 0 useful warnings in 45 labelled; candidate to become opt-in.
- `--github` measured (2026-11-01): precision unchanged, +7 useful warnings, almost no demotions.
- Tried and dropped (2026-11-01): "fix word + PR number" as an offline fix rule: +85 warnings at 22%
  useful. Don't retry it without a narrower signal (see bench/RESULTS.md).
- Known recall limit: tracing only passes commits classified as refactors, so a big non-refactor rewrite
  hides older fixes (e.g. Flask `c2705ffd` hides fix #2935 in `ctx.py` `copy()`). Diagnosed 2026-11-18:
  `c2705ffd` really changed those tokens (`self.session` → `self._session`), so no "unchanged line" rule
  can reach #2935.
- Tried and dropped (2026-11-18): hop past non-refactor commits when the line survived them unchanged in
  the same file (normalized or KeptLines). Precision 54.6% → 52.8%: lost 8 warnings (4 useful, e.g. Express's
  open-redirect fix), added 6 (1 useful). `map` on app.py went 1.4 → 3.5 s. Exact-only matching changed no
  warning. `tests/test_trace.py` pins why. Unmeasured leads: KeptLines in the refactor hop (Black's
  trailing commas), renamed attributes.
- Labels match on commit/check/path/cited commit, not line: when a rule moves a warning to another line,
  re-check its label (#40 found two that way).
- Contributors: Sandro850 (#30, #38, #40, #45). Open issues: #2–#4, #6 (bench cases), #33 (guard reports).
- Launch: dev.to post published 2026-10-24; the CVE sentence there should mention the withdrawn advisory.
