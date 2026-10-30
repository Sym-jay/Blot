"""Check 3: you changed A, but B almost always changes with A, and B isn't staged.

Algorithm (association rules over commit history):
    support(A, B)    = number of commits touching both A and B
    confidence(A→B)  = support(A, B) / commits touching A
    warn when confidence >= config.cochange_min_confidence
          and support    >= config.cochange_min_support
          and B exists at HEAD and is not part of the change
Commits touching more than config.cochange_max_files_per_commit files are ignored
(mass reformatting and renames would otherwise link everything to everything).
"""

from __future__ import annotations

from blot.checks.base import Check, Finding, is_skipped
from blot.git import tracked_files


def is_variant(a: str, b: str) -> bool:
    """Two versions of one file, kept apart on purpose: per-build or per-platform forks
    (ReactFeatureFlags.native-fb.js / ReactFeatureFlags.js) or the same file in sibling
    packages (react-server-dom-esm/.../Node.js / react-server-dom-webpack/.../Node.js).
    A source file and its header (foo.c / foo.h) differ in extension, so they still pair."""
    name_a, name_b = a.rsplit("/", 1)[-1], b.rsplit("/", 1)[-1]
    stem_a, _, ext_a = name_a.partition(".")
    stem_b, _, ext_b = name_b.partition(".")
    return stem_a == stem_b and ext_a.rsplit(".", 1)[-1] == ext_b.rsplit(".", 1)[-1]


class ForgotRelatedFile(Check):
    id = "forgot-related-file"
    description = "Changed a file without the file that usually changes with it"

    def run(self, changes, store, config) -> list[Finding]:
        ignored = lambda p: is_skipped(p, config)  # noqa: E731
        staged = {p for c in changes for p in (c.path, c.old_path) if p}
        head_files: set[str] | None = None
        findings: list[Finding] = []
        for change in changes:
            if ignored(change.path):
                continue
            partners = store.partners(change.path, config.cochange_min_support, config.cochange_min_confidence)
            if not partners and change.old_path and change.old_path != change.path:
                partners = store.partners(change.old_path, config.cochange_min_support, config.cochange_min_confidence)
            if partners and head_files is None:
                head_files = tracked_files(store.repo, store.rev)
            for partner, support, confidence in partners:  # strongest first
                if partner in staged or partner not in head_files or ignored(partner) or is_variant(change.path, partner):
                    continue
                findings.append(Finding(
                    self.id, change.path, None,
                    f"{partner} changed together with this file in {confidence:.0%} of its commits "
                    f"({support} commits), but it isn't staged.",
                    severity=1,
                ))
                break  # at most one finding per staged file
        return findings
