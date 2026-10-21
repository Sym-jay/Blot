"""The interface every check implements. Adding a check = one new file + one registry line."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
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


def describe_commit(row: CommitRow) -> str:
    """Evidence line: 'abc1234 "subject" (2020-10-03, fixes #3761)'."""
    details = [row.date[:10]]
    if row.issues:
        details.append("fixes " + ", ".join(row.issues))
    return f'{row.sha[:7]} "{row.subject}" ({", ".join(details)})'
