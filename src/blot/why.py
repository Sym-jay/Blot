"""`blot why FILE:LINE`: the commit that explains a line, seeing past refactors."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

from blot.git import run_git
from blot.store import CommitRow, Store
from blot.trace import trace_lines

_LOCATION = re.compile(r"^(?P<path>.+?):(?P<start>\d+)(?:-(?P<end>\d+))?$")


class WhyError(Exception):
    pass


@dataclass
class Explained:
    line: int
    text: str
    sha: str
    via: list[str]


def parse_location(location: str) -> tuple[str, int, int]:
    m = _LOCATION.match(location)
    if not m:
        raise WhyError(f"expected FILE:LINE or FILE:START-END, got {location!r}")
    start = int(m.group("start"))
    end = int(m.group("end") or start)
    if start < 1 or end < start:
        raise WhyError(f"bad line range in {location!r}")
    return m.group("path"), start, end


def _repo_path(repo: str, path: str) -> str:
    """Path relative to the repo root, as git names it."""
    out = run_git(repo, "ls-files", "--full-name", "--", path).strip().splitlines()
    if not out:
        raise WhyError(f"{path} is not tracked by git")
    return out[0]


def explain(store: Store, location: str, rev: str = "HEAD") -> tuple[str, list[Explained]]:
    path, start, end = parse_location(location)
    path = _repo_path(store.repo, path)
    try:
        lines = run_git(store.repo, "show", f"{rev}:{path}").splitlines()
    except subprocess.CalledProcessError as exc:
        raise WhyError(f"{path} doesn't exist at {rev}") from exc
    if start > len(lines):
        raise WhyError(f"{path} has only {len(lines)} lines at {rev}")
    end = min(end, len(lines))
    texts = {n: lines[n - 1] for n in range(start, end + 1) if lines[n - 1].strip()}
    trail = trace_lines(store, rev, path, texts)
    return path, [Explained(n, texts[n], t.sha, t.via) for n, t in sorted(trail.items())]


def _kind(row: CommitRow | None) -> str:
    if row is None:
        return "not indexed yet: run `blot index`"
    if row.is_refactor:
        return "refactor"
    return {2: "bug fix", 1: "routine fix"}.get(row.severity, "change")


def _commit_line(store: Store, sha: str) -> str:
    row = store.get_commit(sha)
    if row is not None:
        date, subject = row.date[:10], row.subject
    else:
        date, subject = run_git(store.repo, "log", "-1", "--format=%as%x00%s", sha).strip().split("\0", 1)
    refs = ""
    if row is not None and row.issues:
        refs = "  " + ", ".join(store.describe_issue(ref) for ref in row.issues)
    return f"{sha[:8]}  {date}  {subject}  [{_kind(row)}]{refs}"


def render(store: Store, path: str, explained: list[Explained]) -> str:
    if not explained:
        return f"{path}: no lines to explain (blank, or not in history)\n"
    out: list[str] = []
    # consecutive lines with the same story are shown once
    groups: list[list[Explained]] = []
    for e in explained:
        if groups and groups[-1][-1].sha == e.sha and groups[-1][-1].via == e.via and groups[-1][-1].line == e.line - 1:
            groups[-1].append(e)
        else:
            groups.append([e])
    for group in groups:
        first, last = group[0], group[-1]
        where = f"{path}:{first.line}" if first is last else f"{path}:{first.line}-{last.line}"
        out.append(where)
        out.extend(f"  | {e.text.strip()}" for e in group[:6])
        if len(group) > 6:
            out.append(f"  | ... ({len(group) - 6} more lines)")
        out.append(f"  explained by  {_commit_line(store, first.sha)}")
        for sha in first.via:
            out.append(f"  traced past   {_commit_line(store, sha)}")
        out.append("")
    return "\n".join(out)
