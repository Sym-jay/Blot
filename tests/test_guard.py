import io
import json
import os
import subprocess
import sys
import time

import pytest

import blot.checks.removed_fix as removed_fix_module
from blot import guard, hooks
from blot.index import build_index
from test_checks import AFTER, FIXED, GUARD, sha


@pytest.fixture
def fixed_repo(make_repo):
    repo = make_repo(FIXED)
    build_index(str(repo))
    return repo


GUARD_DOCSTRING = '''\
def render():
    """Render safely.
    helper(documented=True)
    """
    value = helper(executable=True)
    name = "render"
    return value
'''


@pytest.fixture
def docstring_repo(make_repo):
    repo = make_repo([
        ("Add render", {"app.py": 'def render():\n    name = "render"\n    return 1\n'}),
        ("Fix render crash (fixes #1)", {"app.py": GUARD_DOCSTRING}),
    ])
    build_index(str(repo))
    return repo


def payload(repo, tool, **tool_input):
    return {
        "session_id": "s", "transcript_path": "t.jsonl", "cwd": str(repo), "permission_mode": "default",
        "hook_event_name": "PreToolUse", "tool_name": tool, "tool_use_id": "toolu_1",
        "tool_input": {"file_path": str(repo / "app.py"), **tool_input},
    }


def run_guard(data, ask=False):
    out = io.StringIO()
    stdin = io.StringIO(data if isinstance(data, str) else json.dumps(data))
    assert guard.run(stdin, out, ask=ask) == 0
    return out.getvalue()


def test_edit_of_the_fix_line_warns_and_cites_the_fix(fixed_repo):
    out = run_guard(payload(fixed_repo, "Edit", old_string=GUARD, new_string="self_ref = lambda: self"))
    context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "removed-fix  app.py:3" in context
    assert f'{sha(fixed_repo)[:7]} "Break reference cycle in App (fixes #3761)"' in context


def test_edit_of_an_unrelated_line_is_quiet(fixed_repo):
    assert run_guard(payload(fixed_repo, "Edit", old_string="class App:", new_string="class Application:")) == ""


def test_write_that_drops_the_fix_line_warns(fixed_repo):
    out = run_guard(payload(fixed_repo, "Write", content=AFTER.replace(f"        {GUARD}\n", "")))
    assert "removed-fix" in out


def test_multiedit_applies_edits_in_order(fixed_repo):
    edits = [
        {"old_string": "class App:", "new_string": "class Application:"},
        {"old_string": "class Application:\n    def __init__(self):\n        self_ref", "new_string": "class Application:\n    def __init__(self):\n        ref"},
    ]
    assert "removed-fix  app.py:3" in run_guard(payload(fixed_repo, "MultiEdit", edits=edits))
    # an edit that can't apply means the tool call fails anyway: nothing to say
    assert run_guard(payload(fixed_repo, "MultiEdit", edits=[{"old_string": "nope", "new_string": "x"}])) == ""


def test_readded_line_warns(fixed_repo):
    out = run_guard(payload(fixed_repo, "Edit", old_string="self_ref().send()", new_string="self.send()"))
    assert "readded-bug" in out


def test_line_numbers_follow_uncommitted_edits(fixed_repo):
    # two lines added above the fix since HEAD: the warning names the line the agent sees
    (fixed_repo / "app.py").write_text("import weakref\n\n" + AFTER, encoding="utf-8")
    out = run_guard(payload(fixed_repo, "Edit", old_string=GUARD, new_string="self_ref = self"))
    assert "app.py:5" in out


def test_guard_docstrings_use_head_lines_and_keep_executable_findings(docstring_repo, monkeypatch):
    # Guard maps these working-tree lines back to HEAD before RemovedFix checks docstring ranges.
    (docstring_repo / "app.py").write_text("# local change\n\n" + GUARD_DOCSTRING, encoding="utf-8")
    original = removed_fix_module.run_git
    reads = 0

    def count_historical_reads(repo, *args):
        nonlocal reads
        reads += 1
        return original(repo, *args)

    monkeypatch.setattr(removed_fix_module, "run_git", count_historical_reads)
    assert run_guard(payload(
        docstring_repo, "Edit", old_string="helper(documented=True)",
        new_string="helper(documented=False)",
    )) == ""
    assert reads == 1

    reads = 0
    out = run_guard(payload(
        docstring_repo, "Edit", old_string="value = helper(executable=True)",
        new_string="value = helper(executable=False)",
    ))
    assert "removed-fix  app.py:7" in out
    assert reads == 1


def test_guard_does_not_read_history_for_ineligible_lines(docstring_repo, monkeypatch):
    monkeypatch.setattr(
        removed_fix_module, "run_git",
        lambda *args: pytest.fail("an ineligible trace must not read the historical file"),
    )
    assert run_guard(payload(
        docstring_repo, "Edit", old_string='name = "render"', new_string='name = "renderer"',
    )) == ""


def test_ask_mode_asks_the_user(fixed_repo):
    out = json.loads(run_guard(payload(fixed_repo, "Edit", old_string=GUARD, new_string="x = 1"), ask=True))
    spec = out["hookSpecificOutput"]
    assert spec["permissionDecision"] == "ask" and "removed-fix" in spec["permissionDecisionReason"]


def test_output_matches_the_hook_contract(fixed_repo):
    out = json.loads(run_guard(payload(fixed_repo, "Edit", old_string=GUARD, new_string="x = 1")))
    # warn mode: context only. No permissionDecision, so the normal permission flow is untouched
    assert out == {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": out["hookSpecificOutput"]["additionalContext"]}}


def test_quiet_for_untracked_files_missing_index_and_bad_input(fixed_repo, make_repo, tmp_path):
    (fixed_repo / "new.py").write_text(f"{GUARD}\n", encoding="utf-8")
    data = payload(fixed_repo, "Edit", old_string=GUARD, new_string="x = 1")
    data["tool_input"]["file_path"] = str(fixed_repo / "new.py")
    assert run_guard(data) == ""

    os.remove(fixed_repo / ".git" / "blot.db")
    assert run_guard(payload(fixed_repo, "Edit", old_string=GUARD, new_string="x = 1")) == ""
    assert not (fixed_repo / ".git" / "blot.db").exists()  # no surprise minute-long first index

    outside = tmp_path / "loose.py"
    outside.write_text(f"{GUARD}\n", encoding="utf-8")
    data["tool_input"]["file_path"] = str(outside)
    assert run_guard(data) == ""
    for bad in ("", "not json", "[]", '{"tool_name": "Edit"}', '{"tool_name": "Bash", "tool_input": {}}',
                '{"tool_name": "Edit", "tool_input": {"file_path": 3}}'):
        assert run_guard(bad) == ""


def test_cli_guard_reads_stdin_and_stays_fast(fixed_repo):
    data = json.dumps(payload(fixed_repo, "Edit", old_string=GUARD, new_string="self_ref = self"))
    start = time.perf_counter()
    done = subprocess.run([sys.executable, "-m", "blot", "guard"], input=data, capture_output=True, text=True)
    elapsed = time.perf_counter() - start
    assert done.returncode == 0 and "removed-fix" in done.stdout
    assert elapsed < 5  # generous for CI; the real budget (~500 ms) is checked on Flask by hand


# --- install-agent-hook ---------------------------------------------------------------------


def test_install_agent_hook_keeps_other_hooks_and_uninstall_restores_them(make_repo):
    repo = str(make_repo([("init", {"a.py": "x = 1\n"})]))
    path = hooks.agent_settings_path(repo)
    os.makedirs(os.path.dirname(path))
    original = {"model": "opus", "hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "lint.sh"}]}]}}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(original, f)

    assert hooks.install_agent(repo, python="C:\\Py\\python.exe") == "installed"
    assert hooks.install_agent(repo, python="C:\\Py\\python.exe") == "already installed"
    settings = json.load(open(path, encoding="utf-8"))
    groups = settings["hooks"]["PreToolUse"]
    assert groups[0] == original["hooks"]["PreToolUse"][0]
    assert groups[1]["matcher"] == "Edit|Write|MultiEdit"
    assert groups[1]["hooks"][0]["command"] == '"C:/Py/python.exe" -m blot guard'

    assert hooks.install_agent(repo, python="C:\\Py\\python.exe", ask=True) == "updated"
    assert json.load(open(path, encoding="utf-8"))["hooks"]["PreToolUse"][1]["hooks"][0]["command"].endswith("--ask")

    assert hooks.uninstall_agent(repo) == "removed"
    assert json.load(open(path, encoding="utf-8")) == original
    assert hooks.uninstall_agent(repo) == "not installed"


def test_install_agent_hook_creates_and_deletes_its_own_file(make_repo):
    repo = str(make_repo([("init", {"a.py": "x = 1\n"})]))
    assert hooks.install_agent(repo, python="/usr/bin/python3", local=True) == "installed"
    path = hooks.agent_settings_path(repo, local=True)
    assert path.endswith("settings.local.json") and os.path.exists(path)
    assert hooks.uninstall_agent(repo, local=True) == "removed"
    assert not os.path.exists(path)


def test_install_agent_hook_refuses_unreadable_settings(make_repo):
    repo = str(make_repo([("init", {"a.py": "x = 1\n"})]))
    path = hooks.agent_settings_path(repo)
    os.makedirs(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        f.write("{ not json")
    with pytest.raises(ValueError):
        hooks.install_agent(repo)
    assert open(path, encoding="utf-8").read() == "{ not json"
