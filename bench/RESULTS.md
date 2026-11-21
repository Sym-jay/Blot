# Bench results

Measured 2026-10-30 with `blot bench`, on Windows 11, Python 3.12.

## Cases: 33/33 correct

| Check | Cases | Precision | Recall |
|---|---|---|---|
| removed-fix | 24 (16 warn, 8 quiet) | 100% | 100% |
| readded-bug | 6 (4 warn, 2 quiet) | 100% | 100% |
| forgot-related-file | 3 (3 quiet) | n/a | n/a |

A quiet case now fails if its scripted edit can't be applied (it used to pass without running).

The 21 cases added in this round are real commits from the labelled replays below: each one replays
that commit's own hunk on its parent. Quiet cases pin the noise patterns fixed here so they stay fixed.

## Replay: last 500 non-merge commits of 5 repos, every warning labelled

Each commit is checked as if it were being committed at that moment, using only the history before
it. Every warning was then labelled by hand as **useful** or **noise** (rules in
[README.md](README.md#labels-precision-you-can-re-measure)); the labels live in [`labels/`](labels/),
so these numbers can be re-measured after any rule change. "Before" is Blot 0.4.0; "after" is this
release.

| Repo | Labelled at | Commits | Warnings / 10 commits, before → after | Precision, before → after |
|---|---|---|---|---|
| pallets/flask | `d086db856` | 500 | 0.10 → **0.08** | 3/5 (60%) → **3/4 (75%)** |
| psf/requests | `611c6162c` | 500 | 0.16 → **0.16** | 2/8 (25%) → **2/8 (25%)** |
| expressjs/express | `0dee2c52c` | 498 | 0.18 → **0.18** | 5/9 (56%) → **5/9 (56%)** |
| django/django | `79502d084` | 497 | 1.29 → **1.19** | 40/64 (63%) → **40/59 (68%)** |
| facebook/react | `9ffcb463b` | 483 | 1.26 → **0.60** | 7/61 (11%) → **7/29 (24%)** |
| **All** | | 2,478 | 0.59 → **0.44** | 57/147 (39%) → **57/109 (52%)** |

The rule changes removed 38 warnings, all of them labelled noise; no useful warning was lost.

**Against the PRD targets:** at most 1 warning per 10 commits is met everywhere except Django (1.19).
Precision of 80% is **not** met: 52% overall, 68% on Django, where most warnings are fixes touching
code from earlier fixes and CVEs.

### What a useful warning looks like

- Django `8fd29079e` relaxed the POST permission check that the CVE-2019-19118 fix added.
- Django `7ff7fcc05` redid the `parse_header_parameters` simplification that an earlier commit had
  reverted as a bug (#36520). Both `removed-fix` and `readded-bug` fired.
- Flask `cabda59` changed `bp_subdomain is None` to `is not None` on a line from fix #4834; a later
  commit had to fix the behaviour.
- Express `697547cd` reverted the CVE-2024-51999 patch. That revert was deliberate (the advisory,
  GHSA-pj86-cfqh-vqx6, was later withdrawn), so it is useful as "a security fix is being undone",
  not as a caught regression.

### Noise this release removed

| Noise (warnings) | Fix |
|---|---|
| React feature-flag forks, per-bundler and per-platform copies of one file (27) | co-change skips variants: same name stem and extension (`ReactFeatureFlags.native-fb.js` ↔ `ReactFeatureFlags.js`); `foo.c` ↔ `foo.h` still pair |
| deleting a whole file (4) | `removed-fix` skips deleted files |
| commits cited as fixes that weren't: `Fixed #21189: Cleaned up ...`, `Fixed #12882 - Moved ...`, `[DevTools] upgrade to Manifest V3`, `Change Flask.__init__ to accept ...`, error-message wording (6) | `Fixed #N:` / `Fixed #N -` ticket styles judged by their description; cleanups count as refactors; `[Tag]` prefixes and "change" subjects count as features; error messages are routine |
| docstrings with `backticks` or (asides) (1) | they count as prose |

### Since 0.6.0: real docstrings (#40, by @Sandro850)

`removed-fix` now parses the old Python file (`ast`, only when a warning is already on its way) and skips
lines inside real module, class and function docstrings. That removed one Django noise warning. It also
exposed two of **our** labels as wrong: in Flask `c2705ffd` and Django `ebcb13b3`, the warning used to point at
a docstring line, but the same change also edits the fix's code (Flask's `copy()` passing
`session=self.session`, from fix #2935; Django's `isinstance(normalized, (dict, str))` check, from the
CVE-2026-15307 fix). Now the warning points at the code, and both are relabelled useful.

| Repo | Before | After |
|---|---|---|
| pallets/flask | 3/4 (75%) | **4/4 (100%)** |
| django/django | 40/59 (68%) | **41/58 (71%)** |
| requests, Express, React | unchanged | unchanged |
| **All five** | 57/109 (52.3%) | **59/108 (54.6%)** |

### Since 0.7.0: a structural matcher (#45, by @Sandro850)

`removed-fix` decides whether a removed line survives among the added lines (`KeptLines`). The old
matcher accepted any inserted token outside `_LOGIC`, which flagged an appended keyword argument (it
inserts `=`) but let real edits through: `value` → `value.strip()`, arguments inserted mid-list,
matches stitched across hunks. The new matcher is structural: the fix's existing arguments stay token
for token, new arguments may only be appended to that same call (a `def` only gains parameters with a
default), and anything it can't verify counts as an edit.

| Repo | Before | After |
|---|---|---|
| pallets/flask | 4/4 (100%) | 4/4 (100%) |
| psf/requests | 2/8 (25%) | 2/10 (20%) |
| expressjs/express | 5/9 (56%) | 5/9 (56%) |
| django/django | 41/58 (71%), 1.17 / 10 commits | 49/71 (69%), 1.43 / 10 commits |
| facebook/react | 7/29 (24%) | 9/30 (30%) |
| **All five** | 59/108 (54.6%) | **69/124 (55.6%)** |

The three #36 noise warnings are gone and no useful warning was lost. The stricter matcher adds 19
warnings (10 useful, e.g. two Django CVE fixes whose code gained `.strip()`; 9 noise). Re-measured
independently on all five repos before merging. Cost: Django is further over the ≤ 1 / 10 target.

### Noise still there (55 warnings)

| Pattern | Warnings | Why there's no rule yet |
|---|---|---|
| co-change partner not needed (React) | 9 | **0 of 45 co-change warnings across all five repos were useful.** See below. |
| renames, equivalent API swaps (`substr` → `slice`, `req.connection` → `req.socket`) | 11 | a rename and a switch to a different variable look the same on one line |
| formatter reflows with f-strings, joined lines or re-split strings (requests' Black commit) | 6 | dropping parentheses can change precedence |
| deleting a legacy module or feature in pieces (React) | 6 | intentional, but only the commit message says so |
| colour-only template changes (Django's dark mode, `3d34265d`) | 5 | the line really changed; only a reader knows it's cosmetic |
| other: code moved to another file, fix behaviour kept while restructuring, generic re-added lines | 18 | case by case |

**`forgot-related-file` has shown no useful warning yet**: 0/45 before this release, 0/9 after. It
stays on, because the remaining warnings are rare (React only), but it is the first candidate to
become opt-in if real-world reports agree.

## Does `--github` help? (measured 2026-11-01)

`blot bench --replay 500 --github` reads issue and PR labels the way `blot index --github` does.
Flask, requests, Express and React track bugs on GitHub; Django's GitHub Issues are off (it uses
Trac), so it is unchanged. New warnings were labelled with the same rules.

| Repo | Without `--github` | With `--github` | Commits re-judged by labels |
|---|---|---|---|
| pallets/flask | 3/4 (75%) | 3/4 (75%) | 0 |
| psf/requests | 2/8 (25%) | **4/10 (40%)** | 15 upgraded |
| expressjs/express | 5/9 (56%) | 5/9 (56%) | 25 upgraded |
| facebook/react | 7/29 (24%) | **12/40 (30%)** | 154 upgraded, 4 demoted |
| **All five, with Django** | 57/109 (52.3%) | **64/122 (52.5%)** | |

**`--github` doesn't raise precision; it finds more useful warnings** (7 more, with 5 more noise).
Labels almost never demote a commit (4 of ~3,200 issues and PRs looked up). What they do is
upgrade commits like `ReactDOM: Fix missing form data ... (#28056)`, which Blot rates as unclear
without labels: "Fix" isn't at the start, and there's no "fixes #N" or strong defect word. 

Caveat: labels are read as they are today, so a label added after a commit was made counts too.

### Tried and dropped: "a fix word plus a PR number" (2026-11-01)

Could a rule do offline what the `bug` label does? Rule tried: a fix word anywhere in the subject
plus an issue or PR number makes a commit a fix. It added **85 warnings** (React went from 0.60 to
1.95 per 10 commits), because almost every React and Express subject ends in its PR number. Of 46
labelled (all of requests' and Express's, 20 of React's), **10 were useful (22%)**. Most were
`fix(deps): …@^2.0.0` bumps, "Fix Flow type" commits and import lines. Overall precision would fall
from 52% to about 39%, so the rule was not kept. Its 46 labels stay in `labels/` for any narrower
attempt. A label written by a maintainer says more than a keyword can.

### Tried and dropped: tracing past unchanged lines (2026-11-18)

**The gap.** At Flask HEAD, `blot why` and `blot map src/flask/ctx.py` don't link `copy()` to fix
#2935 (`e08bcf9`). Blame stops at `c2705ffd` ("merge app and request context"), and tracing only goes
past commits classified as refactors.

**Diagnosis: `c2705ffd` really rewrote those lines.** It changed `request=self.request,` /
`session=self.session,` to `request=self._request,` / `session=self._session,` (the session became
a lazy property backed by `_session`), dropped `environ=self.request.environ,`, and changed the
signature to `def copy(self) -> te.Self:`. `git blame -w -M -C -C` also stops at `c2705ffd`. No
normalized or KeptLines match exists in the parent, so no rule of the form "the line survived
unchanged" can reach #2935 here. The fix's intent survives; its tokens don't. Replay already warns on
`c2705ffd` itself (labelled useful), which is when it matters most.

**Rule tried anyway, to measure it:** when blame lands on a non-refactor commit that removed a line
from the same file with the same normalized text (or one the line only reformats, per `KeptLines`),
hop to the parent like a refactor hop, within `MAX_HOPS`.

| Repo | Warnings | Precision, before → with the rule | Lost (useful) | New (useful) |
|---|---|---|---|---|
| pallets/flask | 4 → 4 | 4/4 → 3/4 | 1 (1) | 1 (0) |
| psf/requests | 8 → 9 | 2/8 → 2/9 | 0 | 1 (0) |
| expressjs/express | 9 → 7 | 5/9 → 4/7 | 2 (1) | 0 |
| django/django | 58 → 58 | 41/58 → 40/58 | 4 (2) | 4 (1) |
| facebook/react | 29 → 28 | 7/29 → 7/28 | 1 (0) | 0 |
| **All five** | **108 → 106** | **59/108 (54.6%) → 56/106 (52.8%)** | **8 (4)** | **6 (1)** |

It loses useful warnings because `KeptLines` treats an inserted call as cosmetic, so the rule hops
past fixes that wrapped a value. Express `76eaa32` changed `this.set('Location', loc)` to
`this.set('Location', encodeUrl(loc))`, so the warning on the later open-redirect fix
(`0867302dd`) disappeared. Flask lost the `c2705ffd` warning: `e08bcf9` only added a comma to
`request=self.request`, and the line it did add, `session=self.session`, gained a trailing comma
in Black's `025589ee`, which the exact-match refactor hop can't follow. The one useful new warning
is Django `1b4d021b6`. Cost: `blot check` on the Flask demo went from ~470 to ~485 ms (median of 6),
and `blot map src/flask/app.py` from 1.4 s to 3.5 s.

An **exact-text-only variant** (no `KeptLines`) changed **no warning** on any of the five repos and still cost ~35 ms per check. Neither version was kept. The six new warnings stay
labelled in `labels/` (reasons prefixed "trace-past-unchanged experiment"). `tests/test_trace.py`
pins both behaviours: a rewritten line stops the trace, and a fix that wraps a value stays the
explanation.

Two narrower leads came out of this, both unmeasured: let the refactor hop match with `KeptLines`
(Black's trailing commas, as in `025589ee`), and follow renamed attributes (`self.session` →
`self._session`).

## Reproduce

```bash
git clone https://github.com/pallets/flask && cd flask && git checkout d086db856
blot bench --replay 500 --labels path/to/Blot/bench/labels/pallets-flask.json
```
