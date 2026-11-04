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


# --- Claude Code agent hook -------------------------------------------------------------------
# A PreToolUse hook in .claude/settings.json runs `blot guard` before every Edit, Write and
# MultiEdit. We find our entry again by its command, so other hooks in the file are left alone.

AGENT_MATCHER = "Edit|Write|MultiEdit"
_GUARD_MARK = "-m blot guard"


def agent_settings_path(repo: str, local: bool = False) -> str:
    top = run_git(repo, "rev-parse", "--show-toplevel").strip()
    return os.path.normpath(os.path.join(top, ".claude", "settings.local.json" if local else "settings.json"))


def _agent_command(python: str, ask: bool) -> str:
    py = python.replace("\\", "/")
    return f'"{py}" {_GUARD_MARK}' + (" --ask" if ask else "")


def _is_guard(hook: object) -> bool:
    return isinstance(hook, dict) and _GUARD_MARK in str(hook.get("command", ""))


def _read_settings(path: str) -> dict:
    import json

    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        text = f.read()
    settings = json.loads(text) if text.strip() else {}
    if not isinstance(settings, dict):
        raise ValueError(f"{path} is not a JSON object")
    return settings


def _write_settings(path: str, settings: dict) -> None:
    import json

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")


def install_agent(repo: str, python: str | None = None, ask: bool = False, local: bool = False) -> str:
    """Add the guard to Claude Code's settings. Returns 'installed', 'updated' or 'already installed'.

    Raises ValueError if the settings file isn't valid JSON: we never overwrite what we can't read.
    """
    path = agent_settings_path(repo, local)
    settings = _read_settings(path)
    command = _agent_command(python or sys.executable, ask)
    groups = settings.setdefault("hooks", {}).setdefault("PreToolUse", [])
    for group in groups:
        for hook in group.get("hooks", []) if isinstance(group, dict) else []:
            if _is_guard(hook):
                if hook.get("command") == command:
                    return "already installed"
                hook["command"] = command  # e.g. switching to --ask, or a new virtualenv
                _write_settings(path, settings)
                return "updated"
    groups.append({"matcher": AGENT_MATCHER, "hooks": [{"type": "command", "command": command, "timeout": 10}]})
    _write_settings(path, settings)
    return "installed"


def uninstall_agent(repo: str, local: bool = False) -> str:
    """Remove the guard's entry. Deletes the settings file only if nothing else is left in it."""
    path = agent_settings_path(repo, local)
    if not os.path.exists(path):
        return "not installed"
    settings = _read_settings(path)
    hooks = settings.get("hooks")
    groups = hooks.get("PreToolUse") if isinstance(hooks, dict) else None
    if not isinstance(groups, list):
        return "not installed"
    found, kept = False, []
    for group in groups:
        if isinstance(group, dict) and isinstance(group.get("hooks"), list):
            others = [h for h in group["hooks"] if not _is_guard(h)]
            if len(others) < len(group["hooks"]):
                found = True
                if not others:
                    continue
                group = {**group, "hooks": others}
        kept.append(group)
    if not found:
        return "not installed"
    if kept:
        hooks["PreToolUse"] = kept
    else:
        del hooks["PreToolUse"]
        if not hooks:
            del settings["hooks"]
    if settings:
        _write_settings(path, settings)
    else:
        os.remove(path)
    return "removed"
