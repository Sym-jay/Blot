"""Turn `git diff` output into per-file added and removed lines with line numbers."""

from __future__ import annotations

import hashlib
import re
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


def is_prose_line(normalized: str) -> bool:
    """Comments and docstring text: a fix that reworded them is not what Blot guards."""
    if normalized.startswith(_COMMENT_START):
        return True
    return len(normalized.split()) >= 5 and not _CODE_HINT.search(normalized)


_TOKEN = re.compile(r"\w+|[^\w\s]")


def tokens(normalized: str) -> list[str]:
    """Code tokens with quote style unified, so 'x' and "x" compare equal."""
    return _TOKEN.findall(normalized.replace("'", '"'))


# Tokens that change what code does. Inserting one of these is an edit, not a reformat:
# `x is None` -> `x is not None` must still count as removing the old line.
# Literals like None are left out on purpose: type hints insert them (`data=None` ->
# `data: dict | None = None`), and `not` already catches `is None` -> `is not None`.
_LOGIC = frozenset({"not", "and", "or", "is", "in", "=", "!", "<", "+", "*", "/", "%", "&", "^", "~", "?"})


def _cosmetic_match(needle: list[str], hay: list[str]) -> bool:
    """`needle` appears in `hay` in order, and nothing inserted between its tokens changes logic."""
    matched, pos = [], 0
    for tok in needle:
        try:
            pos = hay.index(tok, pos)
        except ValueError:
            return False
        matched.append(pos)
        pos += 1
    inserted = set(range(matched[0], matched[-1] + 1)) - set(matched)
    return not any(hay[i] in _LOGIC for i in inserted)


class KeptLines:
    """Answers "does this removed line survive among the added lines?"

    A removed line counts as kept when a run of up to `window` consecutive added lines contains all
    of its tokens in order, with no logic-changing token inserted between them. That covers
    reformatting (quotes, re-wrapping) and type hints (`def f(self):` -> `def f(self) -> int:`),
    while removals and logic edits (`is None` -> `is not None`) still count.
    """

    def __init__(self, added: list[str], window: int = 5):
        self.lines = [tokens(normalize_line(text)) for text in added]
        self.window = window
        self.where: dict[str, list[int]] = {}
        for i, toks in enumerate(self.lines):
            for tok in set(toks):
                self.where.setdefault(tok, []).append(i)

    def keeps(self, removed: str) -> bool:
        needle = tokens(normalize_line(removed))
        if not needle:
            return True
        # start from the lines holding the needle's rarest token
        rare = min(needle, key=lambda t: len(self.where.get(t, ())))
        for i in self.where.get(rare, ()):
            for start in range(max(0, i - self.window + 1), i + 1):
                hay = [t for line in self.lines[start:start + self.window] for t in line]
                if _cosmetic_match(needle, hay):
                    return True
        return False


def is_meaningful_line(normalized: str) -> bool:
    """Worth checking against history: real code, not blank, generic or prose."""
    return bool(normalized) and not is_trivial_line(normalized) and not is_prose_line(normalized)


def line_hash(normalized: str) -> str:
    return hashlib.blake2b(normalized.encode("utf-8"), digest_size=8).hexdigest()


@dataclass
class FileChange:
    path: str  # path after the change (old path for deleted files)
    old_path: str | None  # path before the change (None for new files)
    removed: list[tuple[int, str]] = field(default_factory=list)  # (old line number, text)
    added: list[tuple[int, str]] = field(default_factory=list)  # (new line number, text)


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
            current = FileChange(path=new_path or "", old_path=old_path)
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
