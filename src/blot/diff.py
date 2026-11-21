"""Turn `git diff` output into per-file added and removed lines with line numbers."""

from __future__ import annotations

import ast
import hashlib
import re
from bisect import bisect_left
from dataclasses import dataclass, field

from blot.git import run_git

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_TRAILING_COMMENT = re.compile(r"\s+(?:#|//)\s.*$")
_TRIVIAL = re.compile(r"^(?:[\s{}()\[\];,.:]*|(?:import|from|#include|using|package|require)\b.*|(?:return|pass|break|continue|else|end|try|finally)\b\W*)$")


def normalize_line(text: str) -> str:
    """Collapse whitespace and drop trailing comments so cosmetic edits still match."""
    return " ".join(_TRAILING_COMMENT.sub("", text).split())


def is_trivial_line(normalized: str) -> bool:
    """Lines too generic to mean anything on their own: brackets, imports, bare keywords."""
    return len(normalized) < 12 or bool(_TRIVIAL.match(normalized))


_COMMENT_START = ("#", "//", "/*", "*", "--", ":param", ":return", ":raises", "@param", "@return", '"""', "'''")
_CODE_HINT = re.compile(
    r"[=()\[\]{};<>_]|\w\.\w|^(?:if|elif|else|for|while|return|def|class|with|assert|raise|try|except|"
    r"const|let|var|function|import|export|switch|case|throw)\b"
)
_PROSE_ASIDE = re.compile(r"`[^`]+`|(?<=\s)\(\w[\w\s,]*\)")


def is_prose_line(normalized: str) -> bool:
    """Comments and docstring text: a fix that reworded them is not what Blot guards."""
    if normalized.startswith(_COMMENT_START):
        return True
    # docstrings quote names in backticks and add asides in parentheses: "nested (WKT) or `x`"
    text = _PROSE_ASIDE.sub(" ", normalized)
    return len(text.split()) >= 5 and not _CODE_HINT.search(text)


# A string literal is one token, so brackets, commas and `=` inside it are never read as code.
_TOKEN = re.compile(r"""\w+|"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`(?:[^`\\]|\\.)*`|[^\w\s]""")


def tokens(normalized: str) -> list[str]:
    """Code tokens, each string literal one token. Quote style is unified, so 'x' and "x" compare
    equal, and `def f(a, b: "x=y")` has no `=` outside its string."""
    out = []
    for tok in _TOKEN.findall(normalized):
        if tok[0] == "'":
            tok = f'"{tok[1:-1]}"' if len(tok) > 1 else '"'
        out.append(tok)
    return out


# Tokens that change what code does. Inserting one of these is an edit, not a reformat:
# `x is None` -> `x is not None` must still count as removing the old line.
# Literals like None are left out on purpose: type hints insert them (`data=None` ->
# `data: dict | None = None`), and `not` already catches `is None` -> `is not None`.
_LOGIC = frozenset({"not", "and", "or", "is", "in", "=", "!", "<", "+", "*", "/", "%", "&", "^", "~", "?"})


_OPENERS = {"(": ")", "[": "]", "{": "}"}
_CLOSERS = {")": "(", "]": "[", "}": "{"}
# words a bracket can follow without calling or indexing anything: `if (x)`, `return [a]`
_KEYWORDS = frozenset({
    "if", "elif", "while", "for", "switch", "catch", "return", "yield", "await", "async", "and", "or",
    "not", "in", "is", "of", "with", "assert", "else", "except", "lambda", "typeof", "case", "del",
    "raise", "throw", "new", "void", "delete", "instanceof", "import", "from",
})


def _depths(toks: list[str]) -> tuple[list[int], int]:
    """Bracket depth before each token (a closer counts at its outer depth), and the final depth."""
    depth, out = 0, []
    for tok in toks:
        if tok in _CLOSERS:
            depth -= 1
        out.append(depth)
        if tok in _OPENERS:
            depth += 1
    return out, depth


def _balanced(toks: list[str]) -> bool:
    depths, end = _depths(toks)
    return end == 0 and min(depths, default=0) >= 0


def _top_comma(toks: list[str]) -> bool:
    """A comma outside every bracket in `toks`."""
    depths, _ = _depths(toks)
    return any(tok == "," and depth == 0 for tok, depth in zip(toks, depths))


def _pairs(toks: list[str]) -> dict[int, int]:
    """Opener index <-> closer index for every bracket pair that closes in `toks`."""
    pairs: dict[int, int] = {}
    stack: list[int] = []
    for i, tok in enumerate(toks):
        if tok in _OPENERS:
            stack.append(i)
        elif tok in _CLOSERS and stack and toks[stack[-1]] == _CLOSERS[tok]:
            j = stack.pop()
            pairs[i], pairs[j] = j, i
    return pairs


def _operand(tok: str) -> bool:
    """Can a `(` or `[` right after `tok` call or index it?"""
    return tok in (")", "]") or tok[0] in "\"`" or (tok.isidentifier() and tok not in _KEYWORDS)


def _list_kind(toks: list[str], pairs: dict[int, int], j: int) -> str | None:
    """What the `(` at `toks[j]` opens: "def" (Python), "function" (a JS function or method),
    "call", or None for anything else: grouping, class bases, `f?.(`, `f<T>(`."""
    if toks[j] != "(" or j == 0:
        return None
    prev, before = toks[j - 1], toks[j - 2] if j >= 2 else ""
    if prev == "]":
        # PEP 695 type parameters (`def f[T](`, `class C[T](`), or else a subscript being called
        # (`factory[T](`, `handlers[0](`)
        start = pairs.get(j - 1)
        if start is None:
            return None
        name, keyword = toks[start - 1] if start else "", toks[start - 2] if start >= 2 else ""
        if keyword in ("def", "class") and name.isidentifier():
            return "def" if keyword == "def" else None
    elif before == "def" and prev.isidentifier():
        return "def"
    if prev == "function" or (before == "function" and prev.isidentifier()):
        return "function"
    if before == "class" or not _operand(prev):
        return None
    after = toks[pairs[j] + 1:pairs[j] + 2] if j in pairs else []
    if after == ["{"] or (after == [":"] and toks[-1] == "{"):
        return "function"  # `name(a) {` or `name(a): T {`: a JS method definition
    return "call"


def _items(toks: list[str]) -> list[list[str]]:
    """Split on top-level commas, dropping empty items (trailing commas)."""
    depths, _ = _depths(toks)
    items: list[list[str]] = [[]]
    for tok, depth in zip(toks, depths):
        if tok == "," and depth == 0:
            items.append([])
        else:
            items[-1].append(tok)
    return [item for item in items if item]


def _has_default(param: list[str]) -> bool:
    """`name=value` or `name: T = value`: a parameter callers may leave out."""
    depths, _ = _depths(param)
    return param[0].isidentifier() and any(
        tok == "=" and depth == 0 and param[i - 1] not in ("=", "!", "<", ">") and param[i + 1:i + 2] != ["="]
        for i, (tok, depth) in enumerate(zip(param, depths)) if i
    )


def _is_type(run: list[str]) -> bool:
    """A type expression: balanced, no top-level comma, nothing from `_LOGIC`."""
    return bool(run) and _balanced(run) and not _top_comma(run) and not any(tok in _LOGIC for tok in run)


def _is_annotation(run: list[str], name: str, nxt: str) -> bool:
    """`: T` inserted between a name and the `,` `=` or `)` after it: a type hint."""
    return name.isidentifier() and nxt in (",", "=", ")") and run[0] == ":" and _is_type(run[1:])


def _top_level_ok(needle: list[str], k: int, run: list[str], kinds: dict[int, str | None],
                  pairs: dict[int, int]) -> bool:
    """Outside every bracket only a type hint may be inserted: `x: T = ...`, `def f() -> T:`,
    `function f(): T {`."""
    tok, nxt = needle[k], needle[k + 1]
    if nxt == "=" and needle[k + 2:k + 3] != ["="]:
        return _is_annotation(run, tok, nxt)
    if tok == ")" and kinds.get(pairs.get(k, -1)) in ("def", "function"):
        return ((run[:2] == ["-", ">"] and nxt == ":" and _is_type(run[2:]))
                or (run[0] == ":" and nxt == "{" and _is_type(run[1:])))
    return False


def _wraps(needle: list[str], k: int) -> bool:
    """May a `(` inserted after `needle[k]` wrap the rest of the line? Black adds these around a
    long right-hand side; after `return` or an assignment's `=`, and with no top-level comma that
    would make a tuple, they change nothing."""
    tok = needle[k]
    assignment = tok == "=" and k > 0 and needle[k - 1] not in ("=", "!", "<", ">") and needle[k + 1] != "="
    return (tok == "return" or assignment) and not _top_comma(needle[k + 1:])


def _wraps_condition(needle: list[str], k: int) -> bool:
    """May a `(` inserted right after `if`, `elif` or `while` wrap the whole condition, up to the
    line's final `:`? Black adds these around a long condition; with no top-level comma (a tuple
    is always true) they change nothing."""
    return (k == 0 and needle[0] in ("if", "elif", "while") and needle[-1] == ":" and len(needle) > 2
            and not _top_comma(needle[1:-1]))


def _list_gap_ok(needle: list[str], k: int, run: list[str], open_: list[int], kinds: dict[int, str | None],
                 pairs: dict[int, int]) -> bool:
    """May `run` be inserted between `needle[k]` and `needle[k + 1]`, inside the bracket `open_[-1]`?"""
    inner = open_[-1]
    kind = kinds.get(inner)
    last = pairs[inner] == k + 1  # right before the bracket's closer
    if kind is None:
        # a group, literal, subscript, class bases or unrecognised call: a trailing comma at most,
        # and not one that turns `(x)` into a tuple or `d[x]` into a tuple key
        if run != [","] or not last:
            return False
        opener = needle[inner]
        if opener == "{" or (opener == "[" and not (inner and _operand(needle[inner - 1]))):
            return True
        return _top_comma(needle[inner + 1:pairs[inner]])
    if last:
        if needle[k] in ("(", ","):
            cut = 0
        else:
            depths, _ = _depths(run)
            cut = next((i for i, (tok, d) in enumerate(zip(run, depths)) if tok == "," and d == 0), len(run))
        changed, appended = run[:cut], run[cut:]
    else:
        changed, appended = run, []
    # an existing argument stays token for token; a definition's parameter may gain a type hint
    if changed and not (kind != "call" and _is_annotation(changed, needle[k], needle[k + 1])):
        return False
    if not appended:
        return True
    if not _balanced(appended):
        return False
    new = _items(appended)
    if not new:
        return True  # a trailing comma
    if any(needle[j] == "(" for j in open_[:-1]):
        return False  # a call inside another call's (or group's) parentheses: that argument changed
    if kind == "call":
        return True
    # a definition only gains parameters callers can leave out; a lone `*` may separate them
    if kind == "def" and new.count(["*"]) == 1 and new[-1] != ["*"]:
        new.remove(["*"])
    return all(_has_default(param) for param in new)


def _kept_match(needle: list[str], hay: list[str], rows: list[int]) -> bool:
    """Does `hay` (whole added lines, `rows` saying which) hold the removed line `needle`?

    It must start the first line and end the last one (a JS `;` may follow), its tokens must appear
    in order with each bracket pair still pairing up, and nothing may be inserted but:
    - arguments appended to the end of a call or definition that isn't inside another call's
      parentheses (anything goes inside them; a definition only gains parameters with a default)
    - type hints, trailing commas (never one that makes a tuple), and Black's parentheses around
      the right-hand side of `return` or `=`
    A fragment (a bracket it closes without opening, or opens and keeps writing in) may gain
    nothing: its structure can't be checked. Brackets left open at the very end are fine.
    """
    if not hay or hay[0] != needle[0]:
        return False  # code before the line
    npairs, hpairs = _pairs(needle), _pairs(hay)
    # brackets left open at the end (`x = [`, `function f(a) {`) leave the rest of the line whole;
    # nothing may be inserted after them
    whole = len(needle)
    while whole and needle[whole - 1] in _OPENERS and whole - 1 not in npairs:
        whole -= 1
    fragment = any((tok in _OPENERS or tok in _CLOSERS) and i not in npairs for i, tok in enumerate(needle[:whole]))
    hay_depth = [0]  # hay_depth[i]: brackets open before hay[i]
    for tok in hay:
        hay_depth.append(hay_depth[-1] + (tok in _OPENERS) - (tok in _CLOSERS))
    matched = [0]
    inside = [0] if needle[0] in _OPENERS else []  # needle openers still open
    pos = 1
    for k in range(1, len(needle)):
        tok, j = needle[k], npairs.get(k)
        if j is not None and j < k:  # a closer must close the same bracket in the added code
            pos = hpairs.get(matched[j], -1)
            if pos <= matched[-1]:
                return False
            del inside[inside.index(j):]
        else:
            # within a bracket, a token sits directly inside its counterpart, not deeper:
            # `f(a, b)` -> `f(a: dict[str, int], b)` must not take the comma inside `dict[...]`
            depth = hay_depth[matched[inside[-1]]] + 1 if inside else None
            pos = next((i for i in range(pos, len(hay))
                        if hay[i] == tok and (depth is None or hay_depth[i] == depth)), -1)
            if pos < 0:
                return False
            if tok in _OPENERS:
                inside.append(k)
        matched.append(pos)
        pos += 1

    kinds = {j: _list_kind(needle, npairs, j) for j, tok in enumerate(needle) if tok == "("}
    end = matched[-1]  # the last added token the removed line accounts for
    closer = -1  # where parentheses around an `if` condition must close: right before its `:`
    open_: list[int] = []
    for k in range(len(needle) - 1):
        if needle[k] in _OPENERS:
            open_.append(k)
        elif needle[k] in _CLOSERS and npairs.get(k) in open_:
            del open_[open_.index(npairs[k]):]
        lo, hi = matched[k] + 1, matched[k + 1]
        if lo == hi:
            continue
        run = hay[lo:hi]
        if fragment or k >= whole:
            ok = False
        elif open_:
            ok = _list_gap_ok(needle, k, run, open_, kinds, npairs)
        elif run == ["("] and _wraps(needle, k) and hpairs.get(lo) == matched[-1] + 1:
            ok, end = True, matched[-1] + 1
        elif run == ["("] and _wraps_condition(needle, k) and hpairs.get(lo) == matched[-1] - 1:
            ok, closer = True, matched[-1] - 1
        elif run == [")"] and lo == closer:
            ok = True
        else:
            ok = _top_level_ok(needle, k, run, kinds, npairs)
        if not ok:
            return False
    # code after the line is an edit too, except a JS `;`, or a trailing comma on a line that is
    # already a comma list (`a, b` -> `a, b,` when Black explodes it; `x = 1` -> `x = 1,` is a tuple)
    rest = [hay[i] for i in range(end + 1, len(hay)) if rows[i] == rows[end]]
    return rest in ([], [";"]) or (rest == [","] and not fragment and _top_comma(needle))


class KeptLines:
    """Answers "does this removed line survive among the added lines?"

    A removed line counts as kept when it starts an added line and that line, with the ones after
    it in the same hunk (up to `window`, more while a bracket opened on the first is still open),
    holds it per `_kept_match`: re-wrapped, re-quoted, given type hints or trailing commas, or with
    arguments appended to its call. Anything else counts as removed, including what Blot can't
    verify: code added around the line, or a fragment of a call whose `(` is on another line.

    `numbers` are the added lines' line numbers; lines from separate hunks are never combined.
    """

    def __init__(self, added: list[str], window: int = 5, numbers: list[int] | None = None,
                 max_span: int = 20):
        self.lines = [tokens(normalize_line(text)) for text in added]
        self.window = window
        self.max_span = max_span
        self.block_start: list[int] = []
        for i in range(len(added)):
            new_block = i == 0 or (numbers is not None and numbers[i] != numbers[i - 1] + 1)
            self.block_start.append(i if new_block else self.block_start[-1])
        self.block_end = [0] * len(added)
        for i in reversed(range(len(added))):
            last = i + 1 == len(added) or self.block_start[i + 1] != self.block_start[i]
            self.block_end[i] = i + 1 if last else self.block_end[i + 1]
        self.starts: dict[str, list[int]] = {}  # first token -> lines starting with it
        self.where: dict[str, list[int]] = {}  # token -> lines holding it, in order
        for i, toks in enumerate(self.lines):
            if toks:
                self.starts.setdefault(toks[0], []).append(i)
            for tok in dict.fromkeys(toks):
                self.where.setdefault(tok, []).append(i)

    def _hay(self, start: int) -> tuple[list[str], list[int]]:
        """Tokens of the lines from `start`, and the line each one came from."""
        stop = self.block_end[start]
        hay: list[str] = []
        rows: list[int] = []
        depth, i = 0, start
        while i < stop and (i < start + self.window or (depth > 0 and i < start + self.max_span)):
            for tok in self.lines[i]:
                hay.append(tok)
                rows.append(i)
                depth += (tok in _OPENERS) - (tok in _CLOSERS)
            i += 1
        return hay, rows

    def keeps(self, removed: str) -> bool:
        needle = tokens(normalize_line(removed))
        if not needle:
            return True
        # try the lines it could start, skipping those with its rarest token nowhere in reach
        rare = self.where.get(min(needle, key=lambda t: len(self.where.get(t, ()))), [])
        for start in self.starts.get(needle[0], ()):
            i = bisect_left(rare, start)
            reach = min(self.block_end[start], start + self.max_span)
            if i < len(rare) and rare[i] < reach and _kept_match(needle, *self._hay(start)):
                return True
        return False


def is_meaningful_line(normalized: str) -> bool:
    """Worth checking against history: real code, not blank, generic or prose."""
    return bool(normalized) and not is_trivial_line(normalized) and not is_prose_line(normalized)


def python_docstring_lines(source: str) -> set[int] | None:
    """Line numbers occupied by real Python docstrings, or None if parsing fails."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return None

    lines: set[int] = set()
    scopes = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if not isinstance(node, scopes) or not node.body:
            continue
        first = node.body[0]
        if not (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            continue
        end = first.value.end_lineno or first.value.lineno
        lines.update(range(first.value.lineno, end + 1))
    return lines


def line_hash(normalized: str) -> str:
    return hashlib.blake2b(normalized.encode("utf-8"), digest_size=8).hexdigest()


@dataclass
class FileChange:
    path: str  # path after the change (old path for deleted files)
    old_path: str | None  # path before the change (None for new files)
    removed: list[tuple[int, str]] = field(default_factory=list)  # (old line number, text)
    added: list[tuple[int, str]] = field(default_factory=list)  # (new line number, text)
    deleted: bool = False  # the whole file is removed


def parse_unified_diff(text: str) -> list[FileChange]:
    """Parse `git diff -U0` output. Binary files and mode-only changes produce no lines."""
    changes: list[FileChange] = []
    current: FileChange | None = None
    old_path: str | None = None
    old_no = new_no = 0
    for line in text.splitlines():
        if line.startswith("--- "):
            old_path = None if line == "--- /dev/null" else line[6:]  # strip "--- a/"
        elif line.startswith("+++ "):
            new_path = old_path if line == "+++ /dev/null" else line[6:]  # strip "+++ b/"
            current = FileChange(path=new_path or "", old_path=old_path, deleted=line == "+++ /dev/null")
            changes.append(current)
        elif current is None:
            continue
        elif m := _HUNK.match(line):
            old_no, new_no = int(m.group(1)), int(m.group(3))
        elif line.startswith("-"):
            current.removed.append((old_no, line[1:]))
            old_no += 1
        elif line.startswith("+"):
            current.added.append((new_no, line[1:]))
            new_no += 1
    return changes


def read_changes(repo: str, rev_range: str | None = None) -> list[FileChange]:
    """Staged changes by default, or the changes in a commit range like `origin/main..HEAD`."""
    args = ["diff", "-U0", "--no-color", "--no-ext-diff", "-M"]
    args.append(rev_range if rev_range else "--cached")
    return parse_unified_diff(run_git(repo, *args))


def range_base(repo: str, rev_range: str) -> str:
    """The commit `git diff <rev_range>` compares against: A for `A..B`, the merge base for `A...B`."""
    if "..." in rev_range:
        left, right = rev_range.split("...", 1)
        return run_git(repo, "merge-base", left or "HEAD", right or "HEAD").strip()
    if ".." in rev_range:
        return rev_range.split("..", 1)[0] or "HEAD"
    return rev_range
