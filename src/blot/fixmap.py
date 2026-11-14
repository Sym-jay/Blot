"""`blot map FILE`: the lines of a file that exist because of a bug fix.

A line is load-bearing when the commit that explains it (traced past refactors, as `blot why` does)
is a fix at or above `min_severity`. The lines considered are the ones `removed-fix` would guard:
real code, not blank lines, generic lines (`return`, imports, brackets), prose or Python docstrings.
So the map shows where an edit would make `blot check` warn.

One blame of the whole file, plus one blame per refactor hop: about 1 s on Flask's 1,600-line app.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from blot.checks.base import describe_trail
from blot.diff import is_meaningful_line, normalize_line, python_docstring_lines
from blot.store import Store
from blot.trace import trace_lines
from blot.why import commit_info, read_file


@dataclass
class Span:
    """Lines from one fix, next to each other once blank and generic lines are skipped."""

    start: int
    end: int
    lines: list[int]  # the load-bearing lines in start..end
    texts: list[str]
    sha: str
    via: list[str] = field(default_factory=list)  # refactors traced past, newest first


@dataclass
class FileMap:
    path: str
    rev: str
    line_count: int
    min_severity: int
    spans: list[Span]
    source: list[str]

    @property
    def fixes(self) -> list[str]:
        """Fix commits in order of first appearance in the file."""
        return list(dict.fromkeys(s.sha for s in self.spans))


def build_map(store: Store, path: str, rev: str = "HEAD", min_severity: int = 2) -> FileMap:
    path, text = read_file(store, path, rev)
    source = text.splitlines()
    docstrings = python_docstring_lines(text) if path.endswith(".py") else None
    texts = {
        n: line for n, line in enumerate(source, 1)
        if is_meaningful_line(normalize_line(line)) and not (docstrings and n in docstrings)
    }
    trail = trace_lines(store, rev, path, texts, whole_file=True) if texts else {}

    spans: list[Span] = []
    previous: int | None = None  # the last line considered: a span only grows across skipped lines
    for n in sorted(trail):
        t = trail[n]
        row = store.get_commit(t.sha)
        if row is not None and not row.is_refactor and row.severity >= min_severity:
            last = spans[-1] if spans else None
            if last is not None and last.sha == t.sha and last.end == previous:
                last.end = n
                last.lines.append(n)
                last.texts.append(source[n - 1])
                last.via.extend(v for v in t.via if v not in last.via)
            else:
                spans.append(Span(n, n, [n], [source[n - 1]], t.sha, list(t.via)))
        previous = n
    return FileMap(path, rev, len(source), min_severity, spans, source)


def _where(span: Span) -> str:
    return str(span.start) if span.start == span.end else f"{span.start}-{span.end}"


def render(store: Store, fmap: FileMap, full: bool = False) -> str:
    """Text for the terminal: one block per range, or with `full` the whole file with a gutter."""
    if full:
        marked = {n: s.sha[:7] for s in fmap.spans for n in s.lines}
        width = len(str(fmap.line_count))
        out = [f"{n:>{width}} {marked.get(n, ''):7} | {line}" for n, line in enumerate(fmap.source, 1)]
        out.append("")
        out.append(_summary(fmap))
        out.extend(_legend(store, fmap))
        return "\n".join(out) + "\n"
    if not fmap.spans:
        return _summary(fmap) + "\n"
    out = [_summary(fmap), ""]
    width = max(len(_where(s)) for s in fmap.spans)
    for span in fmap.spans:
        for i, text in enumerate(span.texts[:4]):
            out.append(f"{_where(span) if i == 0 else '':<{width}}  | {text.strip()}")
        if len(span.texts) > 4:
            out.append(f"{'':<{width}}  | ... ({len(span.texts) - 4} more lines)")
        for line in describe_trail(store.get_commit(span.sha), span.via, store):
            out.append(f"{'':<{width}}    {line}")
        out.append("")
    return "\n".join(out)


def _summary(fmap: FileMap) -> str:
    where = f" at {fmap.rev}" if fmap.rev != "HEAD" else ""
    if not fmap.spans:
        return f"{fmap.path}{where}: no load-bearing lines (no line comes from a fix at severity >= {fmap.min_severity})"
    lines = sum(len(s.lines) for s in fmap.spans)
    n = len(fmap.fixes)
    return (f"{fmap.path}{where}: {lines} load-bearing line{'s' * (lines != 1)} in {len(fmap.spans)} "
            f"range{'s' * (len(fmap.spans) != 1)}, from {n} fix{'es' * (n != 1)}")


def _legend(store: Store, fmap: FileMap) -> list[str]:
    via = {sha: [v for s in fmap.spans if s.sha == sha for v in s.via] for sha in fmap.fixes}
    return ["  " + "\n    ".join(describe_trail(store.get_commit(sha), list(dict.fromkeys(via[sha])), store))
            for sha in fmap.fixes]


def to_json(store: Store, fmap: FileMap) -> dict:
    """The map as JSON: ranges in file order, and the commits they cite."""
    cited = list(dict.fromkeys([*fmap.fixes, *(v for s in fmap.spans for v in s.via)]))
    return {
        "path": fmap.path,
        "rev": fmap.rev,
        "line_count": fmap.line_count,
        "min_severity": fmap.min_severity,
        "ranges": [
            {"start": s.start, "end": s.end, "lines": s.lines, "text": s.texts, "commit": s.sha, "via": s.via}
            for s in fmap.spans
        ],
        "commits": {sha: commit_info(store, sha) for sha in cited},
    }
