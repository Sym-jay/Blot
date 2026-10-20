# Contributing to Blot

Thanks for helping! There are three easy ways in.

## 1. Add a benchmark case (no deep code knowledge needed)
`bench/cases.json` is a list of real changes from public repos, each with the warning blot
*should* (or should *not*) produce. Each new case is its own PR. See `bench/README.md`.

## 2. Add or improve a check
Every check is one file in `src/blot/checks/` implementing `Check` from `checks/base.py`,
registered in `checks/__init__.py`. A new check needs:
- a unit test using the `make_repo` fixture in `tests/conftest.py`
- at least two bench cases (one that should warn, one that should stay quiet)

## 3. Improve fix detection
`src/blot/classify.py` decides whether a commit is a bug fix or a refactor. Better heuristics
(more languages' commit conventions, issue-tracker formats) are very welcome. Measure the effect
with `blot bench`.

## Dev setup
```bash
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest
```

## Ground rules
- Core stays **stdlib-only on Python 3.11+** (3.10 gets one small dependency, `tomli`, to read config) and the `check` command stays **under 500 ms**. New runtime dependencies need a strong reason.
- Quiet beats clever: a new warning must not raise the false-alarm rate on the bench.

## Releasing (maintainers)
1. Bump `version` in `pyproject.toml` and merge to `main`.
2. Create a GitHub release with tag `vX.Y.Z` matching that version.
3. The `Release` workflow builds and uploads to PyPI as `blot-git` (trusted publishing, no tokens).
