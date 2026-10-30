# Bench results

Measured 2026-10-30 with `blot bench`, on Windows 11, Python 3.12.

## Cases: 30/30 correct

| Check | Cases | Precision | Recall |
|---|---|---|---|
| removed-fix | 24 (16 warn, 8 quiet) | 100% | 100% |
| readded-bug | 3 (3 warn) | 100% | 100% |
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

### Noise still there (52 warnings)

| Pattern | Warnings | Why there's no rule yet |
|---|---|---|
| co-change partner not needed (React) | 9 | **0 of 45 co-change warnings across all five repos were useful.** See below. |
| renames, equivalent API swaps (`substr` → `slice`, `req.connection` → `req.socket`) | 11 | a rename and a switch to a different variable look the same on one line |
| new argument or keyword argument on a fix's call | 3 | adding an argument can change behaviour |
| formatter reflows with f-strings or joined lines (requests' Black commit) | 4 | dropping parentheses can change precedence |
| deleting a legacy module or feature in pieces (React) | 6 | intentional, but only the commit message says so |
| docstring lines that still read as code | 3 | needs to know it's inside a docstring, not just how the line looks |
| other: code moved to another file, fix behaviour kept while restructuring, generic re-added lines | 16 | case by case |

**`forgot-related-file` has shown no useful warning yet**: 0/45 before this release, 0/9 after. It
stays on, because the remaining warnings are rare (React only), but it is the first candidate to
become opt-in if real-world reports agree.

## Reproduce

```bash
git clone https://github.com/pallets/flask && cd flask && git checkout d086db856
blot bench --replay 500 --labels path/to/Blot/bench/labels/pallets-flask.json
```
