# Blot: Product Requirements Document

| | |
|---|---|
| **Status** | Draft v0.1 · 2026-10-19 |
| **Owner** | You (project lead) |
| **One-liner** | A pre-commit check that has read your project's history, and warns when a change removes a fix, re-adds a fixed bug, or forgets a file that always changes too. |
| **License** | MIT, open source from day one |

---

## 1. Problem

Codebases forget *why* things are the way they are. The reasons live in old commits, PRs and issues, and nobody reads those before changing code. That causes three recurring, expensive mistakes:

1. **Removing a fix.** Someone simplifies an odd-looking line that was actually a bug fix, and the bug comes back.
2. **Re-adding a bug.** Someone restores old code (copied from an old branch, a tutorial, or an AI suggestion) that a fix deliberately removed.
3. **Forgetting a related file.** Someone changes `schema.py` but not `migrations/`, or `api.ts` but not `api.test.ts`, even though history shows those always change together.

These mistakes are caught late (in CI, review or production) or never. AI coding agents make them more often because they only see the current code.

**Evidence:** in our tests ([competitors.md](competitors.md)), every existing tool we ran misread a real Flask case. It stopped at a 2023 refactor commit instead of the 2020 bug fix that explains the code.

## 2. Users

| User | Situation | What they need |
|---|---|---|
| **Engineer on an old codebase** (primary) | Changes code written by people who've left | A heads-up *before* committing, with a link to the original bug |
| **Open-source maintainer** | Reviews PRs from strangers | The same check in CI on incoming PRs (`check --range`) |
| **Team using AI agents** | Agents commit code they don't understand | Agent commits go through the same pre-commit check |
| **New contributor** | Doesn't know the project's history | A way to ask "why is this here?" (`blot why`) |

## 3. Goals and non-goals

**Goals (v0.1)**
- G1. Catch the three mistakes above at commit time, locally, in under 500 ms.
- G2. Stay **quiet**: on typical repos, at most 1 warning per 10 commits, and at least 80% of warnings judged useful on the bench.
- G3. Install in one command on Windows, macOS and Linux. Stdlib-only Python on 3.11+ (3.10 also installs `tomli`).
- G4. Every warning cites real evidence (commit SHA, and PR or issue when available). Nothing is made up.

**Non-goals (v0.1)**
- No AI or LLM in the check path. AI explanations come later and are optional.
- Not a linter, style checker or security scanner.
- Not a hosted service. No accounts, no telemetry.
- Doesn't replace tests or CI. It's an earlier, cheaper signal.

## 4. Competitive positioning

| | On-demand "why" (code-archaeologist, Codebase Time Machine) | Agent edit-time guard (git-fence, lore) | **Blot** |
|---|---|---|---|
| When it runs | When you ask | When an AI agent edits | **Every commit (people and agents)** |
| Works without AI agents | Yes | No (agent hook only) | **Yes** |
| Removed-fix check | Indirect | Yes (git-fence) | Yes, **and traces past refactors** |
| Re-added-bug check | No | No | **Yes** |
| Forgot-related-file check | No | No | **Yes** |
| Windows | Broken / web | WSL only / yes | **Native** |
| Accuracy is measured | No | Self-reported (git-fence) | **Public bench in repo** |

**Our wedge:** commit time + checks 2 and 3 + a public bench. Possible collaboration: git-fence's classifier ideas are MIT-licensed and worth studying (with credit).

## 5. Features (v0.1)

### F1. `blot index`: build the history index
- Reads `git log --numstat -M` and stores the result in `.git/blot.db` (SQLite). It's never committed.
- Classifies each commit: fix severity 0, 1 or 2, and whether it's a refactor.
- Stores normalized hashes of lines that load-bearing fixes removed.
- Mines co-change pairs (support and confidence), skipping commits that touch more than 30 files.
- `--github`: maps commits to PRs and issues with `gh api` (optional; uses the user's own `gh` login).
- Incremental by default: only new commits since the last run.

### F2. `blot check`: run the checks
- Default: staged changes (`git diff --cached`). `--range A..B` for CI and PR checks.
- Exit 0 with printed warnings (default). `--strict` exits 1 when there are findings.
- `--format json` for CI annotations and other tools.
- If there's no index, prints a hint and exits 0. **It never breaks a commit because of its own problems.**

### F3. The three checks

| ID | Fires when | Key precision rules |
|---|---|---|
| `removed-fix` | Removed or edited lines blame back to a fix with severity at or above `min_severity` | Trace past refactor commits (up to 3 hops); ignore whitespace-only changes (`-w`) |
| `readded-bug` | An added line matches a line that a load-bearing fix removed | Skip trivial lines (under 12 chars, brackets, imports); skip if the line still exists in the file at HEAD |
| `forgot-related-file` | Staged file A has a partner B with confidence ≥ 0.8 and support ≥ 5, and B isn't staged | B must exist at HEAD; at most 1 finding per file; respect `ignore_paths` |

### F4. `blot install-hook`
- Writes `.git/hooks/pre-commit`. If a hook already exists, it **appends** a call and never overwrites.
- Also ships `.pre-commit-hooks.yaml` for users of the [pre-commit](https://pre-commit.com) framework.
- Optional `post-commit` and `post-merge` hooks keep the index fresh.

### F5. `blot why FILE:LINE` (v0.1: basic)
- Prints the commit chain for a line, with refactors skipped, plus PR and issue links when indexed.

### F6. `blot bench`
- Runs `bench/cases.json` and prints precision and recall per check. CI fails if precision drops.

## 6. User flows

**First run (2 minutes):**
```
pip install blot-git
cd my-repo
blot index            # ~10–60 s once
blot install-hook
git commit ...              # checks now run automatically
```

**A warning appears:**
```
blot: 1 warning
  ! removed-fix  src/flask/app.py:355
    You're changing a line added by a bug fix.
    8efea0cc "Break reference cycle created by default in Flask instances" (2020-10-03, fixes #3761)
```
The user either reads the linked issue and adjusts the change, or commits anyway (warnings never block by default). They can also silence a pair or path in config.

**CI on pull requests:**
`blot check --range origin/main..HEAD --format json` posts annotations (GitHub Action in M4).

## 7. Requirements

**Functional**
- FR1. Parse staged and range diffs, including renames, new files and deleted files.
- FR2. Classify commits: fix severity (0/1/2) and whether it's a refactor.
- FR3. The three checks, as specified in F3.
- FR4. Config via `[tool.blot]` in `pyproject.toml` or `.blot.toml`: thresholds, `disabled_checks`, `ignore_paths`.
- FR5. Text and JSON output.

**Non-functional**
- NFR1. **Speed:** `check` under 500 ms at p95 on repos with up to 50k commits after indexing. Full index under 60 s for 50k commits; incremental under 1 s.
- NFR2. **Quiet:** false-alarm rate on the bench's quiet cases at most 10%.
- NFR3. **Safety:** never exit non-zero unless `--strict`. Any internal error prints one line and exits 0.
- NFR4. **Privacy:** no network access except `index --github`. Nothing leaves the machine.
- NFR5. **Portability:** Python 3.10+. No runtime dependencies on 3.11+; on 3.10, `tomli` reads config files (stdlib `tomllib` arrived in 3.11). Windows, macOS and Linux tested in CI.

## 8. Architecture

```
 git history ──► index.py ──► classify.py ──► store.py (.git/blot.db)
                                                   │
 git diff --cached ──► diff.py ──► checks/* ◄──────┘
                                      │
                                 report.py ──► terminal / JSON / CI
```

| Module | Role |
|---|---|
| `git.py` | The only place that runs `git` (easy to fake in tests) |
| `diff.py` | Unified diff → `FileChange` (removed and added lines with numbers) |
| `classify.py` | Fix severity + refactor detection. **Most of the precision lives here** |
| `index.py` / `store.py` | Build and query the SQLite index |
| `checks/` | One file per check, all implementing `Check.run(changes, store, config)` |
| `report.py` | Output formatting |

**Extension point:** new checks are a single file plus a registry line. That's the main way people will contribute.

## 9. Scope: v0.1 vs later

| v0.1 (this PRD) | Later |
|---|---|
| Checks 1–3, local index, CLI, git hook, pre-commit framework support, bench of 30 cases | **Check 4: tests likely to break**, using CI history from GitHub Actions |
| `--github` PR and issue links | **AI explanations (RAG):** summarize the PR or issue discussion behind a warning, with citations; local model via Ollama or an API key |
| GitHub only | GitLab and Bitbucket links; mailing lists (Linux, Postgres) |
| | GitHub Action that comments on PRs |
| | VS Code extension that shows warnings as you edit |
| | MCP server, so AI agents can ask before editing |

## 10. Success metrics

| Metric | v0.1 target | How measured |
|---|---|---|
| Bench precision (all checks) | ≥ 80% | `blot bench` |
| False alarms on quiet cases | ≤ 10% | `blot bench` |
| Warnings per 10 commits (5 big public repos) | ≤ 1 | Replay the last 500 commits of each repo |
| `check` latency p95 | < 500 ms | Same replay |
| Adoption (3 months after launch) | 100 stars, 10 external contributors, 3 public repos using it | GitHub |

## 11. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Too many false alarms → users uninstall | High | Quiet defaults, quiet bench cases, severity threshold, easy per-path ignore |
| Fix classification wrong on repos with vague commit messages ("fix stuff") | Medium | Severity 2 needs a strong signal (issue link, defect keyword, bug label); otherwise stay quiet |
| Blame is slow on huge files or repos | Medium | Blame only changed ranges, cache results by blob SHA, time budget with fallback to silence |
| GitHub API rate limits with `--github` | Low | Optional, cached, uses the user's `gh` login |
| A competitor (git-fence) adds commit-time checks | Medium | Move fast on checks 2–3 and the bench; offer to collaborate rather than compete |

## 12. Milestones (4 weeks, part-time)

| Week | Milestone | Done when |
|---|---|---|
| 1 | **M1: index + config + hook installer** | `blot index` builds the DB on Flask in under 10 s; `install-hook` is safe with existing hooks |
| 2 | **M2: three checks** | All unit tests pass, including the "trace past refactor" test (currently skipped) |
| 3 | **M3: `why` + `--github` + output polish** | The Flask case prints the 2020 fix with the #3761 link |
| 4 | **M4: bench + launch** | 30 bench cases, precision ≥ 80%, PyPI release, README GIF, Show HN / r/Python post |

## 13. Open questions

1. Should `readded-bug` also match *near* copies (small edits), or only exact normalized lines? Start exact; measure.
2. Should co-change be computed per directory as well as per file (e.g. `models/*` ↔ `migrations/*`)?
3. What's the default severity for commits labeled `security`? Probably always 2, even without issue links.
4. Name: the project is Blot. `blot` is taken on PyPI (a static site generator), so the package installs as `blot-git` while the import and command stay `blot`. Register `blot-git` early.
