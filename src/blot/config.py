"""Settings, read from `[tool.blot]` in pyproject.toml and/or a `.blot.toml` file.

`.blot.toml` wins over pyproject.toml. Keys may use dashes or underscores:

    [tool.blot]
    min-severity = 2
    cochange-min-confidence = 0.8
    disabled-checks = ["forgot-related-file"]
    ignore-paths = ["vendor/*"]
    check-tests = false   # tests and docs are skipped unless you opt in
    check-docs = false

TOML is read with the stdlib `tomllib` on Python 3.11+ and with `tomli` on 3.10 (a dependency there only).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field, fields


@dataclass
class Config:
    # removed-fix / re-added-bug: only warn for commits classified at or above this severity
    min_severity: int = 2
    # forgot-related-file: confidence that B changes when A changes, and minimum joint commits
    cochange_min_confidence: float = 0.8
    cochange_min_support: int = 5
    # skip commits touching more files than this when mining co-change (mass reformat, renames)
    cochange_max_files_per_commit: int = 30
    disabled_checks: list[str] = field(default_factory=list)
    ignore_paths: list[str] = field(default_factory=list)
    # test and docs files are skipped by default: replays on real repos showed most warnings
    # there were noise (fix commits that also touched tests or docs)
    check_tests: bool = False
    check_docs: bool = False


def _read_toml(path: str) -> dict:
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        try:
            import tomli as tomllib
        except ModuleNotFoundError:
            print(f"blot: ignoring {os.path.basename(path)}: reading it on Python 3.10 needs tomli", file=sys.stderr)
            return {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        print(f"blot: ignoring {os.path.basename(path)}: {exc}", file=sys.stderr)
        return {}


def load_config(repo: str) -> Config:
    raw: dict = {}
    pyproject = os.path.join(repo, "pyproject.toml")
    if os.path.exists(pyproject):
        raw.update(_read_toml(pyproject).get("tool", {}).get("blot", {}))
    own = os.path.join(repo, ".blot.toml")
    if os.path.exists(own):
        raw.update(_read_toml(own))

    config = Config()
    known = {f.name: f for f in fields(Config)}
    for key, value in raw.items():
        name = key.replace("-", "_")
        if name not in known:
            print(f"blot: unknown config key {key!r} (ignored)", file=sys.stderr)
            continue
        default = getattr(config, name)
        expected = type(default)
        if expected is float and isinstance(value, int):
            value = float(value)
        if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
            print(f"blot: config key {key!r} should be {expected.__name__} (ignored)", file=sys.stderr)
            continue
        setattr(config, name, value)
    return config
