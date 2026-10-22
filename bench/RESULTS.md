# Bench results

Measured 2026-10-22 with `blot bench`, on Windows 11, Python 3.12.

## Cases: 9/9 correct

| Check | Cases | Precision | Recall |
|---|---|---|---|
| removed-fix | 6 (3 warn, 3 quiet) | 100% | 100% |
| readded-bug | 2 (2 warn) | 100% | 100% |
| forgot-related-file | 1 (1 quiet) | n/a | n/a |

Nine cases is a start, not a verdict. Each new case is a good first issue (see [README.md](README.md)).

## Replay: last 500 non-merge commits of 5 repos

Each commit is checked as if it were being committed at that moment, using only the history before
it. "Before" is Blot 0.1.0's rules; "after" is this release.

| Repo | Commits | Before: warnings / 10 commits | After | Commits with a warning (after) | Avg check time |
|---|---|---|---|---|---|
| pallets/flask | 500 | 1.96 | **0.10** | 5 (1.0%) | 106 ms |
| psf/requests | 500 | 1.12 | **0.16** | 3 (0.6%) | 52 ms |
| expressjs/express | 498 | 1.00 | **0.18** | 7 (1.4%) | 49 ms |
| django/django | 497 | 26.30 | **1.29** | 50 (10.1%) | 479 ms |
| facebook/react | 483 | 10.00 | **1.30** | 39 (8.1%) | 821 ms |

Target from the PRD: at most 1 warning per 10 commits. Three repos are well under; Django and React are
slightly over. Check times are in-process (no Python startup) and grow with the size of the change.
The last rule change (type hints that insert `None`) was re-measured only on requests; it can only
remove warnings, so the other four numbers are upper bounds.

## Were the warnings worth reading?

Labeled by hand: every warning for Flask, requests and Express; a sample for Django and React.

- **Flask, 5 warnings, 3 useful.** The best: commit `cabda59` changed
  `bp_subdomain is None` to `is not None` on a line from fix `d7b6c1f` (#4834); a later commit had to
  fix `subdomain_matching=False` behavior. Noise: a line from a feature commit, a docstring.
- **Express, 9 warnings, about 7 useful.** The best: commit `697547cd` **reverted the CVE-2024-51999
  security patch**. Both `removed-fix` and `readded-bug` fired. Others: changes to code from fixes for
  proxy handling (#2252), redirect encoding and charset handling.
- **requests, 8 warnings, 1 useful** (dropping `chardet` from code added by fix #4102). The rest come
  from one mass reformat and one typing commit, which still slip through in a few lines.
- **Django, 64 warnings.** Mostly fixes and CVE follow-ups that touch code from earlier fixes, e.g.
  `Fixed #37403 -- Fixed JSONField __in crash on SQLite` flagged for changing code from
  `#37377 -- Fixed JSONField __in crash`. Useful context for a reviewer, though Django's maintainers
  know their history well.
- **React, 63 warnings.** About half are co-change warnings. Useful when a fix lands in one bundler
  variant (`react-server-dom-webpack`) but not its mirrors (turbopack, parcel); noise for feature-flag
  fork files, which are often edited separately on purpose.

## What the replays changed

Every noise pattern became a rule and, where possible, a bench case:

| Noise | Fix |
|---|---|
| "This fixes #N" on features, refactors, docs (old Flask) | feature/refactor/docs subjects count as routine |
| `Fixed #N -- Added ...` (Django's ticket-first style) | judge the text after `--` |
| tests, docs, lock files, translations, CI config | skipped by default (`check-tests` / `check-docs` to opt in) |
| code moved between files | not a removal or a re-add |
| reformatting and type hints | a line whose tokens survive in order, with nothing logic-changing inserted, is kept |
| imports, bare keywords, comments, docstrings | ignored |
| co-change with hub files | the link must hold both ways (≥ 80% each direction) |
| re-added lines matching fixes in other files | only fixes to the same file count |

## Reproduce

```bash
git clone https://github.com/pallets/flask && cd flask
blot bench --replay 500 --json replay.json
```
