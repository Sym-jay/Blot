"""Warn a coding agent before it edits a line that a bug fix added.

`blot guard` is a Claude Code PreToolUse hook on Edit, Write and MultiEdit. It reads the hook's JSON
from stdin, works out the file as it would be after the edit, and runs the removed-fix and
readded-bug checks on the difference. Findings go back as `additionalContext`, which the agent sees
before the edit runs; with `--ask` they also make Claude Code ask the user to approve the edit.

Line numbers: the checks blame at HEAD, but the agent edits the working tree, which may already
differ from HEAD. Removed lines are mapped from the working tree to HEAD; lines the working tree
added since HEAD have no history yet and are skipped.

The guard never blocks an edit because of its own errors, and prints nothing when it has nothing to
say: outside a git repo, for files HEAD doesn't track, and before `blot index` has been run (a first
index can take a minute, far past a hook's budget).
"""

from __future__ import annotations

import difflib
import json
import os
import subprocess
from typing import TextIO

from blot.checks.readded_bug import ReaddedBug
from blot.checks.removed_fix import RemovedFix
from blot.config import load_config
from blot.diff import FileChange
from blot.git import run_git
from blot.report import render

GUARDED_TOOLS = ("Edit", "Write", "MultiEdit")


def repo_root(path: str) -> str | None:
    """The working tree holding `path`: the nearest parent with a `.git` directory or file."""
    here = os.path.dirname(os.path.abspath(path))
    while True:
        if os.path.exists(os.path.join(here, ".git")):
            return here
        parent = os.path.dirname(here)
        if parent == here:
            return None
        here = parent


def _replace(text: str, old: str, new: str, replace_all: bool = False) -> str | None:
    """Apply one Edit the way the tool does, or None if it would fail (then there is nothing to check)."""
    if not old or old not in text:
        return None
    return text.replace(old, new) if replace_all else text.replace(old, new, 1)


def proposed_text(tool: str, tool_input: dict, current: str) -> str | None:
    """The file's content after the tool call, or None if the call can't apply."""
    if tool == "Write":
        content = tool_input.get("content")
        return content if isinstance(content, str) else None
    if tool == "Edit":
        edits = [tool_input]
    elif tool == "MultiEdit":
        edits = tool_input.get("edits")
        if not isinstance(edits, list):
            return None
    else:
        return None
    text: str | None = current
    for edit in edits:  # MultiEdit applies its edits in order, each to the previous result
        old, new = edit.get("old_string"), edit.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str):
            return None
        text = _replace(text, old, new, bool(edit.get("replace_all")))
        if text is None:
            return None
    return text


def _matching(a: list[str], b: list[str]) -> dict[int, int]:
    """{line in b: line in a} for lines the two versions share (1-based)."""
    if a == b:
        return {i: i for i in range(1, len(a) + 1)}
    mapping: dict[int, int] = {}
    for block in difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks():
        for k in range(block.size):
            mapping[block.b + k + 1] = block.a + k + 1
    return mapping


def edit_change(path: str, head: str, current: str, proposed: str) -> tuple[FileChange, dict[int, int]]:
    """The edit as a FileChange, removed lines numbered as at HEAD; also {HEAD line: working-tree line}."""
    cur, new = current.splitlines(), proposed.splitlines()
    to_head = _matching(head.splitlines(), cur)
    change = FileChange(path=path, old_path=path)
    ops = difflib.SequenceMatcher(None, cur, new, autojunk=False).get_opcodes()
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            continue
        change.removed += [(to_head[n], cur[n - 1]) for n in range(i1 + 1, i2 + 1) if n in to_head]
        change.added += [(n, new[n - 1]) for n in range(j1 + 1, j2 + 1)]
    return change, {h: c for c, h in to_head.items()}


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace", newline="") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def findings_for(payload: dict) -> list:
    """Run the guard's checks on one hook payload. Empty when there is nothing to say."""
    from blot.index import build_index
    from blot.store import Store

    tool, tool_input = payload.get("tool_name"), payload.get("tool_input")
    if tool not in GUARDED_TOOLS or not isinstance(tool_input, dict):
        return []
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return []
    if not os.path.isabs(file_path):
        file_path = os.path.join(payload.get("cwd") or ".", file_path)
    root = repo_root(file_path)
    if root is None or not os.path.exists(Store.db_path(root)):
        return []
    rel = os.path.relpath(os.path.abspath(file_path), root).replace(os.sep, "/")
    try:
        head = run_git(root, "show", f"HEAD:{rel}")
    except subprocess.CalledProcessError:  # untracked, new or outside HEAD: no history to guard
        return []

    # Claude Code's Edit matches old_string regardless of CRLF line endings, so compare without them
    current = _read(file_path).replace("\r\n", "\n")
    proposed = proposed_text(tool, tool_input, current)
    if proposed is None or proposed == current:
        return []
    change, to_current = edit_change(rel, head, current, proposed)
    if not change.removed and not change.added:
        return []

    config = load_config(root)
    build_index(root)  # cheap when current; picks up commits made since the last run
    store = Store.open(root)
    if store is None:
        return []
    try:
        checks = [c() for c in (RemovedFix, ReaddedBug) if c.id not in config.disabled_checks]
        findings = [f for check in checks for f in check.run([change], store, config)]
    finally:
        store.close()
    for f in findings:  # removed-fix numbers lines as at HEAD; the agent sees the working tree
        if f.check == RemovedFix.id and f.line in to_current:
            f.line = to_current[f.line]
    return findings


def hook_output(findings: list, ask: bool = False) -> dict:
    """The PreToolUse JSON: context for the agent, and in ask mode a reason for the user."""
    text = render(findings).rstrip() + "\nRun `blot why FILE:LINE` for the full history before you change it."
    out: dict = {"hookEventName": "PreToolUse", "additionalContext": text}
    if ask:
        out["permissionDecision"] = "ask"
        out["permissionDecisionReason"] = text
    return {"hookSpecificOutput": out}


def run(stdin: TextIO, stdout: TextIO, ask: bool = False) -> int:
    """Entry point for `blot guard`. Always returns 0: a broken guard must not stop the agent."""
    try:
        payload = json.load(stdin)
        findings = findings_for(payload) if isinstance(payload, dict) else []
    except Exception:  # malformed input, git or index trouble: stay out of the way
        return 0
    if findings:
        stdout.write(json.dumps(hook_output(findings, ask)) + "\n")
    return 0
