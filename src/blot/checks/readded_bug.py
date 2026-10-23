"""Check 2: you're adding back a line that a load-bearing bug fix deliberately removed.

Algorithm:
    1. normalize each added line (strip whitespace and trailing comments)
    2. skip trivial lines: shorter than 12 chars, only brackets/keywords, imports
    3. look up its hash in fix_removed_lines (same path, or any path if it was renamed)
    4. skip if an identical line still exists elsewhere in the file at HEAD
       (then the fix did not remove the idea, only one copy)
    5. warn with the fix commit that removed it
"""

from __future__ import annotations

import subprocess

from blot.checks.base import Check, Finding, describe_commit, is_skipped
from blot.diff import is_meaningful_line, line_hash, normalize_line
from blot.git import run_git


def _head_lines(repo: str, path: str, rev: str = "HEAD") -> set[str]:
    try:
        text = run_git(repo, "show", f"{rev}:{path}")
    except subprocess.CalledProcessError:
        return set()
    return {normalize_line(line) for line in text.splitlines()}


class ReaddedBug(Check):
    id = "readded-bug"
    description = "Re-adding a line that a bug fix removed"

    def run(self, changes, store, config) -> list[Finding]:
        findings: list[Finding] = []
        # a line removed elsewhere in the same change was moved, not re-added
        moved = {normalize_line(text) for c in changes for _, text in c.removed}
        for change in changes:
            if is_skipped(change.path, config):
                continue
            # only fixes to this same file count: matches in other files were mostly coincidence
            paths = {change.path, change.old_path} - {None}
            head_lines: set[str] | None = None
            reported: set[str] = set()
            for number, text in change.added:
                norm = normalize_line(text)
                if not is_meaningful_line(norm) or norm in moved:
                    continue
                shas = [sha for p, sha in store.fixes_that_removed(line_hash(norm)) if p in paths]
                rows = [r for r in map(store.get_commit, shas) if r and r.severity >= config.min_severity]
                if not rows:
                    continue
                if head_lines is None:
                    head_lines = _head_lines(store.repo, change.old_path, store.rev) if change.old_path else set()
                if norm in head_lines:
                    continue
                row = max(rows, key=lambda r: r.date)  # the most recent fix that removed it
                if row.sha in reported:
                    continue
                reported.add(row.sha)
                findings.append(Finding(
                    self.id, change.path, number,
                    "You're adding back a line that a bug fix removed.\n"
                    "Check that this doesn't bring the bug back.",
                    [describe_commit(row, store)], row.severity,
                ))
        return findings
