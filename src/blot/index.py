"""Build or incrementally refresh the history index.

Steps:
    1. walk `git log --numstat -M` from the last indexed sha to HEAD (no merges)
    2. classify each commit (classify.py), store severity and refactor flags
    3. for load-bearing fixes, hash the lines they removed   -> fix_removed_lines
    4. count file pairs changed together (skip huge commits) -> file_commits / cochange
Targets: under 60 s for 50k commits on a laptop; incremental refresh under 1 s.
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass

from blot.classify import CommitInfo, fix_severity, is_refactor, linked_issues
from blot.config import Config, load_config
from blot.diff import is_trivial_line, line_hash, normalize_line, parse_unified_diff
from blot.git import run_git
from blot.store import CommitRow, Store

_RECORD, _FIELD = "\x1e", "\x1f"
_BRACE_RENAME = re.compile(r"\{([^{}]*) => ([^{}]*)\}")
_SHOW_BATCH = 200


@dataclass
class IndexResult:
    commits: int
    fixes: int
    seconds: float
    full: bool


def _new_path(numstat_path: str) -> tuple[str, bool]:
    """Resolve rename notation (`a/{x => y}/b` or `old => new`) to the new path."""
    if "=>" not in numstat_path:
        return numstat_path, False
    if "{" in numstat_path:
        path = _BRACE_RENAME.sub(lambda m: m.group(2), numstat_path)
        return path.replace("//", "/"), True
    return numstat_path.split(" => ", 1)[1], True


def _parse_log(text: str) -> list[tuple[CommitInfo, str, list[str]]]:
    """Parse our custom `git log --numstat` format into (info, date, paths)."""
    commits = []
    for record in text.split(_RECORD)[1:]:
        sha, date, subject, rest = record.split(_FIELD, 3)
        body, _, numstat = rest.partition(_FIELD)
        added = removed = renames = 0
        paths: list[str] = []
        for line in numstat.strip().splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            a, r, raw_path = parts
            path, renamed = _new_path(raw_path)
            renames += renamed
            added += int(a) if a.isdigit() else 0
            removed += int(r) if r.isdigit() else 0
            paths.append(path)
        info = CommitInfo(sha, subject, body.strip(), len(paths), added, removed, renames)
        commits.append((info, date, paths))
    return commits


def _is_ancestor(repo: str, older: str, newer: str) -> bool:
    try:
        subprocess.run(
            ["git", "-C", repo, "merge-base", "--is-ancestor", older, newer],
            capture_output=True, check=True,
        )
        return True
    except subprocess.CalledProcessError:
        return False


def _head(repo: str, rev: str = "HEAD") -> str | None:
    try:
        return run_git(repo, "rev-parse", "--verify", "-q", f"{rev}^{{commit}}").strip() or None
    except subprocess.CalledProcessError:
        return None


def _removed_lines(repo: str, shas: list[str]) -> list[tuple[str, str, str]]:
    """(line_hash, path, sha) for every meaningful line the given commits deleted."""
    rows: list[tuple[str, str, str]] = []
    for i in range(0, len(shas), _SHOW_BATCH):
        batch = shas[i : i + _SHOW_BATCH]
        out = run_git(repo, "show", f"--format={_RECORD}%H", "-U0", "--no-color", "--no-ext-diff", "-M", *batch)
        for chunk in out.split(_RECORD)[1:]:
            sha, _, diff_text = chunk.partition("\n")
            for change in parse_unified_diff(diff_text):
                path = change.old_path or change.path
                for _, text in change.removed:
                    norm = normalize_line(text)
                    if not is_trivial_line(norm):
                        rows.append((line_hash(norm), path, sha.strip()))
    return rows


def build_index(
    repo: str,
    full: bool = False,
    github: bool = False,
    config: Config | None = None,
    upto: str = "HEAD",
    db_path: str | None = None,
) -> IndexResult:
    """Index history up to `upto` into `db_path` (default .git/blot.db). Bench replays use both."""
    start = time.perf_counter()
    config = config or load_config(repo)
    head = _head(repo, upto)

    store = Store.open(repo, db_path)
    last = store.get_meta("last_sha") if store else None
    if store:
        store.close()
    rebuild = full or not last or (head is not None and not _is_ancestor(repo, last, head))
    store = Store.create(repo, fresh=rebuild, path=db_path)

    if head is None or (not rebuild and last == head):
        store.set_meta("last_sha", head or "")
        store.commit()
        store.close()
        return IndexResult(0, 0, time.perf_counter() - start, rebuild)

    rev = head if rebuild else f"{last}..{head}"
    fmt = f"--format={_RECORD}%H{_FIELD}%aI{_FIELD}%s{_FIELD}%b{_FIELD}"
    log = run_git(repo, "log", "--no-merges", "-M", "--numstat", fmt, rev)
    parsed = _parse_log(log)

    fix_shas = []
    for info, date, paths in parsed:
        severity = fix_severity(info)
        row = CommitRow(info.sha, date, info.subject, severity, is_refactor(info), info.files_changed, linked_issues(info))
        store.add_commit(row)
        if severity >= 2:
            fix_shas.append(info.sha)
        if 0 < len(paths) <= config.cochange_max_files_per_commit:
            store.add_cochange(paths)

    store.add_fix_removed_lines(_removed_lines(repo, fix_shas))
    store.set_meta("last_sha", head)
    store.commit()
    store.close()
    if github:
        print("blot: --github enrichment arrives in milestone M3; indexed git history only.")
    return IndexResult(len(parsed), len(fix_shas), time.perf_counter() - start, rebuild)
