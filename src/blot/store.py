"""Local SQLite index stored at .git/blot.db. Never committed, never leaves the machine.

Tables:
    meta(key, value)                           schema version, last indexed sha
    commits(sha, date, subject, severity, is_refactor, files, issues)
    fix_removed_lines(line_hash, path, sha)    normalized lines deleted by load-bearing fixes
    file_commits(path, count)                  how many indexed commits touched each file
    cochange(path_a, path_b, support)          commits touching both files (path_a < path_b)
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass

from blot.git import git_dir

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS commits (
    sha TEXT PRIMARY KEY, date TEXT, subject TEXT,
    severity INTEGER, is_refactor INTEGER, files INTEGER, issues TEXT
);
CREATE TABLE IF NOT EXISTS fix_removed_lines (line_hash TEXT, path TEXT, sha TEXT);
CREATE INDEX IF NOT EXISTS fix_removed_lines_hash ON fix_removed_lines (line_hash);
CREATE TABLE IF NOT EXISTS file_commits (path TEXT PRIMARY KEY, count INTEGER);
CREATE TABLE IF NOT EXISTS cochange (
    path_a TEXT, path_b TEXT, support INTEGER, PRIMARY KEY (path_a, path_b)
);
"""


@dataclass
class CommitRow:
    sha: str
    date: str
    subject: str
    severity: int
    is_refactor: bool
    files: int
    issues: list[str]


class Store:
    def __init__(self, conn: sqlite3.Connection, repo: str = "."):
        self.conn = conn
        self.repo = repo  # checks run git here

    @staticmethod
    def db_path(repo: str) -> str:
        return os.path.join(git_dir(repo), "blot.db")

    @classmethod
    def open(cls, repo: str) -> Store | None:
        """Return the index, or None if `blot index` has never been run (or it is outdated)."""
        path = cls.db_path(repo)
        if not os.path.exists(path):
            return None
        store = cls(sqlite3.connect(path), repo)
        if store.get_meta("schema_version") != str(SCHEMA_VERSION):
            store.close()
            return None
        return store

    @classmethod
    def create(cls, repo: str, fresh: bool = False) -> Store:
        path = cls.db_path(repo)
        if fresh and os.path.exists(path):
            os.remove(path)
        store = cls(sqlite3.connect(path), repo)
        if store._table_exists("meta") and store.get_meta("schema_version") != str(SCHEMA_VERSION):
            store.close()
            os.remove(path)
            store = cls(sqlite3.connect(path), repo)
        store.conn.executescript(_SCHEMA)
        store.set_meta("schema_version", str(SCHEMA_VERSION))
        return store

    def close(self) -> None:
        self.conn.close()

    def _table_exists(self, name: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
        return row is not None

    # --- meta ---------------------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        try:
            row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        except sqlite3.OperationalError:
            return None
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    # --- writes (used by index.py) ------------------------------------------------------

    def add_commit(self, row: CommitRow) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO commits VALUES (?, ?, ?, ?, ?, ?, ?)",
            (row.sha, row.date, row.subject, row.severity, int(row.is_refactor), row.files, ",".join(row.issues)),
        )

    def add_fix_removed_lines(self, rows: list[tuple[str, str, str]]) -> None:
        self.conn.executemany("INSERT INTO fix_removed_lines VALUES (?, ?, ?)", rows)

    def add_cochange(self, paths: list[str]) -> None:
        """Record one commit touching `paths` (already filtered for huge commits)."""
        self.conn.executemany(
            "INSERT INTO file_commits VALUES (?, 1) ON CONFLICT(path) DO UPDATE SET count = count + 1",
            [(p,) for p in paths],
        )
        ordered = sorted(set(paths))
        pairs = [(a, b) for i, a in enumerate(ordered) for b in ordered[i + 1 :]]
        self.conn.executemany(
            "INSERT INTO cochange VALUES (?, ?, 1) ON CONFLICT(path_a, path_b) DO UPDATE SET support = support + 1",
            pairs,
        )

    def commit(self) -> None:
        self.conn.commit()

    # --- reads (used by checks) ---------------------------------------------------------

    def get_commit(self, sha: str) -> CommitRow | None:
        row = self.conn.execute("SELECT * FROM commits WHERE sha=?", (sha,)).fetchone()
        if row is None:
            return None
        sha, date, subject, severity, refactor, files, issues = row
        return CommitRow(sha, date, subject, severity, bool(refactor), files, [i for i in issues.split(",") if i])

    def fixes_that_removed(self, line_hash: str) -> list[tuple[str, str]]:
        """(path, sha) of load-bearing fixes that deleted a line with this hash."""
        return self.conn.execute("SELECT path, sha FROM fix_removed_lines WHERE line_hash=?", (line_hash,)).fetchall()

    def partners(self, path: str, min_support: int, min_confidence: float) -> list[tuple[str, int, float]]:
        """Files that usually change with `path`: (partner, support, confidence path→partner)."""
        row = self.conn.execute("SELECT count FROM file_commits WHERE path=?", (path,)).fetchone()
        if not row or row[0] == 0:
            return []
        total = row[0]
        rows = self.conn.execute(
            "SELECT CASE WHEN path_a=? THEN path_b ELSE path_a END, support FROM cochange "
            "WHERE (path_a=? OR path_b=?) AND support >= ?",
            (path, path, path, min_support),
        ).fetchall()
        result = [(p, s, s / total) for p, s in rows if s / total >= min_confidence]
        return sorted(result, key=lambda r: (-r[2], -r[1], r[0]))

    def stats(self) -> dict[str, int]:
        q = lambda sql: self.conn.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "commits": q("SELECT COUNT(*) FROM commits"),
            "fixes": q("SELECT COUNT(*) FROM commits WHERE severity >= 2"),
            "refactors": q("SELECT COUNT(*) FROM commits WHERE is_refactor = 1"),
            "fix_lines": q("SELECT COUNT(*) FROM fix_removed_lines"),
            "pairs": q("SELECT COUNT(*) FROM cochange"),
        }
