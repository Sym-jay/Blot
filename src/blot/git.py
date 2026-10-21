"""Thin wrapper around the git CLI. All git access goes through here so tests can fake it."""

from __future__ import annotations

import os
import subprocess


def git_dir(repo: str) -> str:
    """Absolute path of the repo's git directory.

    Found by walking up from `repo` for `.git` (a directory, or a `gitdir:` file in worktrees and
    submodules), which avoids spawning git: each git process costs ~40 ms on Windows and
    `blot check` runs on every commit. Falls back to `git rev-parse` for anything unusual.
    """
    env_dir = os.environ.get("GIT_DIR")
    if env_dir:
        return os.path.abspath(env_dir)
    here = os.path.abspath(repo)
    while True:
        dot_git = os.path.join(here, ".git")
        if os.path.isdir(dot_git):
            return dot_git
        if os.path.isfile(dot_git):
            with open(dot_git, encoding="utf-8") as f:
                first = f.readline().strip()
            if first.startswith("gitdir:"):
                return os.path.normpath(os.path.join(here, first[len("gitdir:"):].strip()))
            break
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    return run_git(repo, "rev-parse", "--absolute-git-dir").strip()


def run_git(repo: str, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return result.stdout


def tracked_files(repo: str, rev: str = "HEAD") -> set[str]:
    """Paths that exist at `rev` (empty for a repo with no commits yet)."""
    try:
        return set(run_git(repo, "ls-tree", "-r", "--name-only", "-z", rev).split("\0")) - {""}
    except subprocess.CalledProcessError:
        return set()
