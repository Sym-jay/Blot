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


def head_sha(repo: str) -> str | None:
    """The commit HEAD points at, read from .git files (the hook calls this on every commit).

    Handles loose and packed refs, detached HEAD and worktrees (`commondir`). Anything else, such as
    the reftable format, falls back to `git rev-parse`.
    """
    gd = git_dir(repo)
    try:
        with open(os.path.join(gd, "HEAD"), encoding="utf-8") as f:
            head = f.read().strip()
        if not head.startswith("ref: "):
            return head if len(head) == 40 else None
        ref = head[5:]
        bases = [gd]
        common = os.path.join(gd, "commondir")
        if os.path.isfile(common):
            with open(common, encoding="utf-8") as f:
                bases.append(os.path.normpath(os.path.join(gd, f.read().strip())))
        for base in bases:
            loose = os.path.join(base, *ref.split("/"))
            if os.path.isfile(loose):
                with open(loose, encoding="utf-8") as f:
                    return f.read().strip()
            packed = os.path.join(base, "packed-refs")
            if os.path.isfile(packed):
                with open(packed, encoding="utf-8") as f:
                    for line in f:
                        sha, _, name = line.strip().partition(" ")
                        if name == ref:
                            return sha
        if not os.path.isdir(os.path.join(gd, "reftable")):
            return None  # unborn branch: no commits yet
    except OSError:
        pass
    try:
        return run_git(repo, "rev-parse", "--verify", "-q", "HEAD").strip() or None
    except subprocess.CalledProcessError:
        return None


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
