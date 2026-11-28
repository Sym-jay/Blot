"""Find the commit that explains a line, seeing past commits that only moved code.

`git blame` answers "who last touched this line". When that is a refactor (a file split, a rename,
a mass reformat), the reason the line exists is older. For each such hop we look up where the
refactor took the line from and blame again at its parent, up to MAX_HOPS times. The exact
normalized match takes priority over a verified Python keyword-argument comma change.
"""

from __future__ import annotations

import ast
import re
import subprocess
import tokenize
import warnings
from bisect import bisect_left
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from io import StringIO
from typing import TYPE_CHECKING

from blot.diff import FileChange, normalize_line, parse_unified_diff
from blot.git import run_git

if TYPE_CHECKING:
    from blot.store import Store

MAX_HOPS = 3
MAX_BLAME_RANGES = 20  # more `-L` ranges than this are blamed as one span
_COMMA_FILES = 8
_COMMA_SOURCE_LIMIT = 512 * 1024  # characters per snapshot
_KEYWORD_LINE = re.compile(r"[^\W\d]\w*\s*=(?!=)")


def _ranges(numbers: list[int]) -> list[tuple[int, int]]:
    """Collapse sorted line numbers into inclusive (start, end) runs."""
    runs: list[tuple[int, int]] = []
    for n in sorted(set(numbers)):
        if runs and n == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], n)
        else:
            runs.append((n, n))
    return runs


def blame(repo: str, rev: str, path: str, lines: list[int], copies: bool = True,
          span: bool = False) -> dict[int, tuple[str, str, int]]:
    """{line: (sha, path in that commit, line in that commit)} via one porcelain blame call.

    `copies=False` drops `-C` (lines copied from other files): see trace_lines. With `span`, more
    than MAX_BLAME_RANGES runs of lines are blamed as one span (and more lines than asked for come
    back), which keeps the command line short when `blot map` asks about a whole file.
    """
    args = ["blame", "-w", "-M", *(["-C"] if copies else []), "--porcelain"]
    runs = _ranges(lines)
    if span and len(runs) > MAX_BLAME_RANGES:
        runs = [(runs[0][0], runs[-1][1])]
    for start, end in runs:
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


def _call_tokens(tree: ast.AST, lines: list[str], rows: set[int]) -> dict[int, tuple[int, tuple[tuple[int, str], ...]]]:
    """Locate each candidate's call in the tree and compare its ordered tokens without the comma."""
    result = {}
    for position, call in enumerate(ast.walk(tree)):
        if not isinstance(call, ast.Call) or not call.keywords:
            continue
        kw = call.keywords[-1]
        n = kw.lineno
        if (n not in rows or kw.arg is None or kw.end_lineno != n
                or not call.lineno < n or call.end_lineno != n + 1 or lines[n].strip() != ")"):
            continue
        # AST columns are UTF-8 byte offsets. Require a standalone keyword, with at most
        # one comma after its value, so punctuation in its value is always part of the signature.
        raw = lines[n - 1].encode()
        if raw[:kw.col_offset].strip() or raw[kw.end_col_offset:].strip() not in (b"", b","):
            continue
        parts = [line.encode() for line in lines[call.lineno - 1:n + 1]]
        parts[0] = parts[0][call.col_offset:]
        parts[-2] = raw[:kw.end_col_offset]
        parts[-1] = parts[-1][:call.end_col_offset]
        tokens = tuple(
            (token.type, token.string)
            for token in tokenize.generate_tokens(StringIO(b"\n".join(parts).decode()).readline)
            if token.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.ENDMARKER)
        )
        result[n] = position, tokens
    return result


def _comma_pairs(change: FileChange) -> dict[int, tuple[int, str, str]]:
    """Pair changed rows immediately above the same unchanged line in the diff."""
    old_rows = sorted(n for n, _ in change.removed)
    new_rows = sorted(n for n, _ in change.added)
    old_by_text = {text.strip(): (n, text) for n, text in change.removed}
    old_counts = Counter(text.strip() for _, text in change.removed)
    new_counts = Counter(text.strip() for _, text in change.added)
    pairs = {}
    for n, added in change.added:
        text = added.strip()
        if not _KEYWORD_LINE.match(text):
            continue
        alternate = text[:-1] if text.endswith(",") else text + ","
        if old_counts[alternate] != 1 or new_counts[text] != 1:
            continue
        old, removed = old_by_text[alternate]
        old_rank = bisect_left(old_rows, old + 1)
        new_rank = bisect_left(new_rows, n + 1)
        if ((old_rank < len(old_rows) and old_rows[old_rank] == old + 1)
                or (new_rank < len(new_rows) and new_rows[new_rank] == n + 1)
                or old - old_rank != n - new_rank):
            continue
        pairs[n] = old, removed, added
    return pairs


class RefactorOrigins:
    """Where a refactor commit took each line from: normalized text -> [(old path, old line)]."""

    def __init__(self, repo: str):
        self.repo = repo
        self.cache: dict[str, dict[str, list[tuple[str, int]]]] = {}
        self._comma_cache: OrderedDict[tuple[str, str], dict[int, tuple[int, str, str]]] = OrderedDict()

    def _load(self, sha: str) -> dict[str, list[tuple[str, int]]]:
        origins: dict[str, list[tuple[str, int]]] = {}
        try:
            diff = run_git(self.repo, "show", "--format=", "-U0", "--no-color", "--no-ext-diff", "-M", sha)
        except subprocess.CalledProcessError:
            diff = ""
        for change in parse_unified_diff(diff):
            for number, removed in change.removed:
                origins.setdefault(normalize_line(removed), []).append((change.old_path or change.path, number))
        return origins

    def prefetch(self, shas: list[str]) -> None:
        """Read several refactors' diffs at once (each git call costs ~40 ms on Windows)."""
        todo = [sha for sha in dict.fromkeys(shas) if sha not in self.cache]
        for sha, origins in zip(todo, _parallel(self._load, todo)):
            self.cache[sha] = origins

    def find(self, sha: str, path: str, text: str) -> tuple[str, int] | None:
        if sha not in self.cache:
            self.cache[sha] = self._load(sha)
        candidates = self.cache[sha].get(normalize_line(text), [])
        same_path = [c for c in candidates if c[0] == path]
        return (same_path or candidates or [None])[0]

    def find_comma(self, sha: str, path: str, line: int, text: str) -> tuple[int, str] | None:
        """After an exact miss, find the same-file predecessor and its historical text."""
        line_text = text.strip()
        if not path.endswith(".py") or not _KEYWORD_LINE.match(line_text):
            return None
        alternate = line_text[:-1] if line_text.endswith(",") else line_text + ","
        # A single bucket lookup on the existing index. Reject duplicate predecessors
        # across the commit before reading syntax, even if some are in other files.
        candidates = self.cache[sha].get(normalize_line(alternate), ())
        if len(candidates) != 1 or candidates[0][0] != path:
            return None
        key = sha, path
        if key not in self._comma_cache:
            self._comma_cache[key] = self._load_comma_pairs(sha, path)
            if len(self._comma_cache) > _COMMA_FILES:
                self._comma_cache.popitem(last=False)
        self._comma_cache.move_to_end(key)
        match = self._comma_cache[key].get(line)
        if match is not None and match[0] == candidates[0][1] and line_text == match[2].strip():
            return match[0], match[1]
        return None

    def _load_comma_pairs(self, sha: str, path: str) -> dict[int, tuple[int, str, str]]:
        parent = f"{sha}^"
        try:
            diff = run_git(self.repo, "diff", "-U0", "--no-color", "--no-ext-diff", "--no-textconv",
                           "--no-renames", parent, sha, "--", f":(literal){path}")
            changes = parse_unified_diff(diff)
            if len(changes) != 1 or changes[0].old_path != path or changes[0].path != path:
                return {}
            pairs = _comma_pairs(changes[0])
            if not pairs:
                return {}
            old = run_git(self.repo, "show", f"{parent}:{path}")
            new = run_git(self.repo, "show", f"{sha}:{path}")
            if any(len(source) > _COMMA_SOURCE_LIMIT or "\ufffd" in source for source in (old, new)):
                return {}
            # A formatting-only file gives calls stable structural positions even when their
            # openers reflow. The diff anchors and positions independently rule out relocation
            # between identical calls. Tokens also check argument order as written.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                warnings.simplefilter("ignore", DeprecationWarning)
                old_tree = ast.parse(old, type_comments=True)
                new_tree = ast.parse(new, type_comments=True)
                if ast.dump(old_tree) != ast.dump(new_tree):
                    return {}
                # Parsing alone allows invalid constructs such as repeated keywords.
                # The two trees are structurally equal, so validate one before using either.
                compile(old_tree, path, "exec", dont_inherit=True)
            # Python and Git count physical newlines, even when a string contains a
            # Unicode separator that str.splitlines would count as another row.
            old_lines, new_lines = old.split("\n"), new.split("\n")
            old_calls = _call_tokens(old_tree, old_lines, {p[0] for p in pairs.values()})
            new_calls = _call_tokens(new_tree, new_lines, set(pairs))
            return {
                n: (o, removed, added) for n, (o, removed, added) in pairs.items()
                if n in new_calls and new_calls[n] == old_calls.get(o)
                and removed == old_lines[o - 1] and added == new_lines[n - 1]
            }
        except (subprocess.CalledProcessError, SyntaxError, ValueError, RecursionError,
                IndexError, tokenize.TokenError):
            return {}


@dataclass
class Trail:
    sha: str  # the commit that explains the line
    path: str  # the line's path in that commit
    line: int  # the line's number in that commit
    via: list[str] = field(default_factory=list)  # refactor commits traced past, newest first


def _parallel(fn, items: list) -> list:
    if len(items) < 2:
        return [fn(item) for item in items]
    with ThreadPoolExecutor(max_workers=min(8, len(items))) as pool:
        return list(pool.map(fn, items))


def trace_lines(store: Store, rev: str, path: str, texts: dict[int, str],
                origins: RefactorOrigins | None = None, whole_file: bool = False) -> dict[int, Trail]:
    """{line number at `rev`: Trail} for the given lines of `path` (text keyed by line number).

    `whole_file` is for `blot map`, which asks about every line: blames cover spans rather than
    hundreds of `-L` ranges, and the first blame leaves out `-C`. Blaming a whole file with `-C`, git
    credits a line moved by a file split straight to its older commit, while blaming that one line
    (as `blot why` does) stops at the split. Without `-C` both stop at the split, which the refactor
    hop below then traces past, so `map` and `why` tell the same story. On Flask's 24 source files
    the lines marked are the same either way.
    """
    repo = store.repo
    origins = origins or RefactorOrigins(repo)
    first = blame(repo, rev, path, list(texts), copies=not whole_file, span=whole_file)
    trail = {n: Trail(sha, p, o) for n, (sha, p, o) in first.items() if n in texts}
    historical_text: dict[int, str] = {}
    for _ in range(MAX_HOPS):
        refactored = [n for n, t in trail.items() if (row := store.get_commit(t.sha)) is not None and row.is_refactor]
        origins.prefetch([trail[n].sha for n in refactored])
        pending: dict[tuple[str, str], list[tuple[int, int, str | None]]] = {}
        for n in refactored:
            t = trail[n]
            text = historical_text.get(n, texts[n])
            origin = origins.find(t.sha, t.path, text)
            old_text = None
            if origin is None:
                comma = origins.find_comma(t.sha, t.path, t.line, text)
                if comma is None:
                    continue
                old_line, old_text = comma
                origin = t.path, old_line
            pending.setdefault((f"{t.sha}^", origin[0]), []).append((n, origin[1], old_text))
        if not pending:
            break
        keys = list(pending)
        blamed = _parallel(lambda key: blame(repo, key[0], key[1], [o for _, o, _ in pending[key]], span=whole_file), keys)
        for key, earlier in zip(keys, blamed):
            for n, o, old_text in pending[key]:
                if o in earlier:
                    sha, p, line = earlier[o]
                    trail[n] = Trail(sha, p, line, [*trail[n].via, trail[n].sha])
                    if old_text is not None:
                        historical_text[n] = old_text
    return trail
