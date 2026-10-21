"""Thin wrapper around the git CLI. All git access goes through here so tests can fake it."""

from __future__ import annotations

import subprocess


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
