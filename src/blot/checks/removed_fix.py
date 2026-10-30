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

from blot.checks.base import Check, Finding, describe_commit, is_skipped
from blot.diff import KeptLines, is_meaningful_line, normalize_line
from blot.trace import RefactorOrigins, trace_lines

class RemovedFix(Check):
    id = "removed-fix"
    description = "Changing code that a bug fix added"

    def run(self, changes, store, config) -> list[Finding]:
        origins = RefactorOrigins(store.repo)
        findings: list[Finding] = []
        # a line that reappears anywhere in the change was moved, not removed
        moved = {normalize_line(text) for c in changes for _, text in c.added}
        for change in changes:
            # deleting a whole file is a decision nobody makes by accident
            if change.old_path is None or change.deleted or not change.removed or is_skipped(change.old_path, config):
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

            trail = trace_lines(store, store.rev, change.old_path, texts, origins)

            reported: set[str] = set()
            for n in sorted(trail):
                sha, via = trail[n].sha, trail[n].via
                row = store.get_commit(sha)
                if row is None or row.is_refactor or row.severity < config.min_severity or sha in reported:
                    continue
                reported.add(sha)  # one finding per fix per file, at its first line
                evidence = [describe_commit(row, store)]
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
