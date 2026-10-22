"""Check 1: you're deleting or editing lines that a load-bearing bug fix added.

Algorithm:
    1. for each file, take the removed lines (old line numbers)
    2. `git blame -w -M -C --porcelain -L a,b HEAD -- <old_path>` for those ranges
    3. if the blamed commit is a refactor (classify.is_refactor), re-blame at its parent
       on the original path, up to 3 hops
    4. warn if the final commit's severity >= config.min_severity

"The original path" is found in the refactor's own diff: the line it deleted with the same
normalized text, preferring the same path. That is how a file split is followed back.
"""

from __future__ import annotations

import subprocess

from blot.checks.base import Check, Finding, describe_commit, is_skipped
from blot.diff import KeptLines, is_meaningful_line, normalize_line, parse_unified_diff
from blot.git import run_git

MAX_HOPS = 3


def _ranges(numbers: list[int]) -> list[tuple[int, int]]:
    """Collapse sorted line numbers into inclusive (start, end) runs."""
    runs: list[tuple[int, int]] = []
    for n in sorted(set(numbers)):
        if runs and n == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], n)
        else:
            runs.append((n, n))
    return runs


def blame(repo: str, rev: str, path: str, lines: list[int]) -> dict[int, tuple[str, str, int]]:
    """{line: (sha, path in that commit, line in that commit)} via one porcelain blame call."""
    args = ["blame", "-w", "-M", "-C", "--porcelain"]
    for start, end in _ranges(lines):
        args += ["-L", f"{start},{end}"]
    try:
        out = run_git(repo, *args, rev, "--", path)
    except subprocess.CalledProcessError:
        return {}
    result: dict[int, tuple[str, str, int]] = {}
    filenames: dict[str, str] = {}
    sha, orig, final = "", 0, 0
    for line in out.splitlines():
        if line.startswith("\t"):
            result[final] = (sha, filenames.get(sha, path), orig)
        elif line.startswith("filename "):
            filenames[sha] = line[9:]
        else:
            parts = line.split(" ")
            if len(parts) >= 3 and len(parts[0]) == 40 and parts[1].isdigit() and parts[2].isdigit():
                sha, orig, final = parts[0], int(parts[1]), int(parts[2])
    return result


class _RefactorOrigins:
    """Where a refactor commit took each line from: normalized text -> [(old path, old line)]."""

    def __init__(self, repo: str):
        self.repo = repo
        self.cache: dict[str, dict[str, list[tuple[str, int]]]] = {}

    def find(self, sha: str, path: str, text: str) -> tuple[str, int] | None:
        if sha not in self.cache:
            origins: dict[str, list[tuple[str, int]]] = {}
            try:
                diff = run_git(self.repo, "show", "--format=", "-U0", "--no-color", "--no-ext-diff", "-M", sha)
            except subprocess.CalledProcessError:
                diff = ""
            for change in parse_unified_diff(diff):
                for number, removed in change.removed:
                    origins.setdefault(normalize_line(removed), []).append((change.old_path or change.path, number))
            self.cache[sha] = origins
        candidates = self.cache[sha].get(normalize_line(text), [])
        same_path = [c for c in candidates if c[0] == path]
        return (same_path or candidates or [None])[0]


class RemovedFix(Check):
    id = "removed-fix"
    description = "Changing code that a bug fix added"

    def run(self, changes, store, config) -> list[Finding]:
        repo = store.repo
        origins = _RefactorOrigins(repo)
        findings: list[Finding] = []
        # a line that reappears anywhere in the change was moved, not removed
        moved = {normalize_line(text) for c in changes for _, text in c.added}
        for change in changes:
            if change.old_path is None or not change.removed or is_skipped(change.old_path, config):
                continue
            # reformatted or extended in place (quotes, wrapping, type hints): not removed either
            kept = KeptLines([text for _, text in change.added])
            # blank, generic (imports, `return`) and prose lines say nothing about the fix
            texts = {
                n: t for n, t in change.removed
                if is_meaningful_line(normalize_line(t)) and normalize_line(t) not in moved and not kept.keeps(t)
            }
            if not texts:
                continue

            blamed = blame(repo, store.rev, change.old_path, list(texts))
            # line -> (sha, path, line in that commit, refactors traced past)
            trail = {n: (sha, p, o, []) for n, (sha, p, o) in blamed.items() if n in texts}
            for _ in range(MAX_HOPS):
                pending: dict[tuple[str, str], list[tuple[int, int]]] = {}  # (parent rev, path) -> [(line, origin line)]
                for n, (sha, path, orig, via) in trail.items():
                    row = store.get_commit(sha)
                    if row is None or not row.is_refactor:
                        continue
                    origin = origins.find(sha, path, texts[n])
                    if origin is None:
                        continue
                    pending.setdefault((f"{sha}^", origin[0]), []).append((n, origin[1]))
                if not pending:
                    break
                for (rev, path), items in pending.items():
                    earlier = blame(repo, rev, path, [o for _, o in items])
                    for n, o in items:
                        if o in earlier:
                            sha, *_ = trail[n]
                            trail[n] = (*earlier[o], [*trail[n][3], sha])

            reported: set[str] = set()
            for n in sorted(trail):
                sha, _, _, via = trail[n]
                row = store.get_commit(sha)
                if row is None or row.is_refactor or row.severity < config.min_severity or sha in reported:
                    continue
                reported.add(sha)  # one finding per fix per file, at its first line
                evidence = [describe_commit(row)]
                for ref_sha in via:
                    ref = store.get_commit(ref_sha)
                    if ref is not None:
                        evidence.append(f'traced past refactor {ref_sha[:7]} "{ref.subject}"')
                findings.append(Finding(
                    self.id, change.old_path, n,
                    "You're removing or changing a line that a bug fix added.\n"
                    "Check that the bug it fixed can't come back.",
                    evidence, row.severity,
                ))
        return findings
