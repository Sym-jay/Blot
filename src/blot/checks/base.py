"""The interface every check implements. Adding a check = one new file + one registry line."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from blot.config import Config
    from blot.diff import FileChange
    from blot.store import CommitRow, Store


@dataclass
class Finding:
    check: str  # check id, e.g. "removed-fix"
    path: str
    line: int | None  # None for file-level findings (forgot-related-file)
    message: str  # one or two plain-English sentences
    evidence: list[str] = field(default_factory=list)  # 'abc1234 "subject" (date, fixes #12)'
    severity: int = 2


class Check(ABC):
    id: str = ""
    description: str = ""

    @abstractmethod
    def run(self, changes: list[FileChange], store: Store, config: Config) -> list[Finding]:
        """Return findings for the staged changes. Must not call the network; target < 100 ms."""


TEST_PATTERNS = ("test/*", "tests/*", "*/test/*", "*/tests/*", "*/__tests__/*", "__tests__/*",
                 "test_*", "*/test_*", "*_test.*", "*.test.*", "*.spec.*", "*/testing/*", "conftest.py", "*/conftest.py")
DOC_PATTERNS = ("doc/*", "docs/*", "*/docs/*", "*.md", "*.rst", "*.txt", "*.adoc", "*.markdown")


# generated files: their lines come from tools, not from the fix's author
GENERATED_PATTERNS = ("*.lock", "*package-lock.json", "*pnpm-lock.yaml", "*npm-shrinkwrap.json", "*go.sum",
                      "*.min.js", "*.min.css", "*.map", "*.mo", "*.po", "*/locale/*",  # translations
                      ".github/*", ".circleci/*", ".gitlab-ci.yml")  # CI configuration


def is_skipped(path: str, config: Config) -> bool:
    """Paths the checks ignore: generated files, config.ignore_paths, and tests and docs unless opted in."""
    patterns = [*GENERATED_PATTERNS, *config.ignore_paths]
    if not config.check_tests:
        patterns += TEST_PATTERNS
    if not config.check_docs:
        patterns += DOC_PATTERNS
    return any(fnmatch(path, p) for p in patterns)


def describe_commit(row: CommitRow, store: Store | None = None) -> str:
    """Evidence line: '8efea0c "subject" (2020-10-03, fixes #3761 "Memory leak in app")'.

    Issue titles appear once `blot index --github` has fetched them.
    """
    details = [row.date[:10]]
    if row.issues:
        refs = [store.describe_issue(ref) for ref in row.issues] if store else row.issues
        details.append("fixes " + ", ".join(refs))
    return f'{row.sha[:7]} "{row.subject}" ({", ".join(details)})'
