"""Install and remove Blot's git pre-commit hook without clobbering existing hooks.

Blot's lines live between two markers, so we can append to a hook someone else wrote and
later remove exactly what we added. The hook calls this Python interpreter by absolute path,
so it works from IDEs whose PATH lacks your virtualenv, and it skips itself (never blocks a
commit) if that interpreter disappears.
"""

from __future__ import annotations

import os
import stat
import sys

from blot.git import run_git

BEGIN, END = "# >>> blot >>>", "# <<< blot <<<"


def hook_path(repo: str, name: str = "pre-commit") -> str:
    path = run_git(repo, "rev-parse", "--git-path", f"hooks/{name}").strip()
    return os.path.normpath(path if os.path.isabs(path) else os.path.join(repo, path))


def _block(python: str) -> str:
    py = python.replace("\\", "/")
    return (
        f"{BEGIN}\n"
        f'if [ -x "{py}" ]; then "{py}" -m blot check || exit $?; fi\n'
        f"{END}\n"
    )


def install(repo: str, python: str | None = None) -> str:
    """Add Blot to the pre-commit hook. Returns 'installed', 'appended' or 'already installed'."""
    path = hook_path(repo)
    block = _block(python or sys.executable)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            current = f.read()
        if BEGIN in current:
            return "already installed"
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(("" if current.endswith("\n") else "\n") + block)
        result = "appended"
    else:
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write("#!/bin/sh\n" + block)
        result = "installed"
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return result


def uninstall(repo: str) -> str:
    """Remove Blot's block. Deletes the hook file only if nothing else is left in it."""
    path = hook_path(repo)
    if not os.path.exists(path):
        return "not installed"
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines(keepends=True)
    if not any(line.strip() == BEGIN for line in lines):
        return "not installed"
    kept, inside = [], False
    for line in lines:
        if line.strip() == BEGIN:
            inside = True
        elif line.strip() == END:
            inside = False
        elif not inside:
            kept.append(line)
    if "".join(kept).strip() in ("", "#!/bin/sh"):
        os.remove(path)
    else:
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.writelines(kept)
    return "removed"
