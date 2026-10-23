"""Find the commit that explains a line, seeing past commits that only moved code.

`git blame` answers "who last touched this line". When that is a refactor (a file split, a rename,
a mass reformat), the reason the line exists is older. For each such hop we look up where the
refactor took the line from (the line it deleted with the same normalized text, preferring the
same path) and blame again at the refactor's parent, up to MAX_HOPS times.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from blot.diff import normalize_line, parse_unified_diff
from blot.git import run_git

if TYPE_CHECKING:
    from blot.store import Store

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


class RefactorOrigins:
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



@dataclass
class Trail:
    sha: str  # the commit that explains the line
    path: str  # the line's path in that commit
    line: int  # the line's number in that commit
    via: list[str] = field(default_factory=list)  # refactor commits traced past, newest first


def trace_lines(store: Store, rev: str, path: str, texts: dict[int, str],
                origins: RefactorOrigins | None = None) -> dict[int, Trail]:
    """{line number at `rev`: Trail} for the given lines of `path` (text keyed by line number)."""
    repo = store.repo
    origins = origins or RefactorOrigins(repo)
    trail = {n: Trail(sha, p, o) for n, (sha, p, o) in blame(repo, rev, path, list(texts)).items() if n in texts}
    for _ in range(MAX_HOPS):
        pending: dict[tuple[str, str], list[tuple[int, int]]] = {}  # (parent rev, path) -> [(line, origin line)]
        for n, t in trail.items():
            row = store.get_commit(t.sha)
            if row is None or not row.is_refactor:
                continue
            origin = origins.find(t.sha, t.path, texts[n])
            if origin is None:
                continue
            pending.setdefault((f"{t.sha}^", origin[0]), []).append((n, origin[1]))
        if not pending:
            break
        for (parent, origin_path), items in pending.items():
            earlier = blame(repo, parent, origin_path, [o for _, o in items])
            for n, o in items:
                if o in earlier:
                    sha, p, line = earlier[o]
                    trail[n] = Trail(sha, p, line, [*trail[n].via, trail[n].sha])
    return trail
