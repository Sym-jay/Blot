import subprocess

import blot.checks.removed_fix as removed_fix_module
import pytest
from blot.checks.cochange import ForgotRelatedFile
from blot.checks.readded_bug import ReaddedBug
from blot.checks.removed_fix import RemovedFix
from blot.config import Config
from blot.diff import read_changes
from blot.index import build_index
from blot.store import Store

GUARD = "self_ref = weakref.ref(self)"
BEFORE = "class App:\n    def __init__(self):\n        self.view = lambda: self.send()\n"
AFTER = f"class App:\n    def __init__(self):\n        {GUARD}\n        self.view = lambda: self_ref().send()\n"
FIXED = [
    ("Add app", {"app.py": BEFORE, "README.md": "app\n"}),
    ("Break reference cycle in App (fixes #3761)", {"app.py": AFTER}),
]


def sha(repo, rev="HEAD"):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", rev], capture_output=True, text=True).stdout.strip()


def stage(repo, files):
    for name, content in files.items():
        (repo / name).write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)


def run_check(check, repo, config=None):
    build_index(str(repo))
    store = Store.open(str(repo))
    try:
        return check.run(read_changes(str(repo)), store, config or Config())
    finally:
        store.close()


# --- removed-fix ------------------------------------------------------------------------


def test_removed_fix_warns_when_fix_line_is_edited(make_repo):
    repo = make_repo(FIXED)
    stage(repo, {"app.py": AFTER.replace(GUARD, "self_ref = lambda: self")})
    (finding,) = run_check(RemovedFix(), repo)
    assert (finding.check, finding.path, finding.line) == ("removed-fix", "app.py", 3)
    assert finding.evidence[0].startswith(f'{sha(repo)[:7]} "Break reference cycle in App (fixes #3761)" (')
    assert finding.evidence[0].endswith(", fixes #3761)")


def test_removed_fix_quiet_for_whitespace_and_non_fix_lines(make_repo):
    repo = make_repo(FIXED + [("Add flag", {"app.py": AFTER + "        self.ok = True\n"})])
    # re-indenting the fix line and editing a line from a feature commit are both fine
    stage(repo, {"app.py": AFTER.replace(f"        {GUARD}", f"        {GUARD}  ") + "        self.ok = False\n"})
    assert run_check(RemovedFix(), repo) == []


def test_removed_fix_respects_min_severity(make_repo):
    repo = make_repo([("Add app", {"app.py": BEFORE}), ("fix typo", {"app.py": BEFORE + "        self.name = 'app'\n"})])
    stage(repo, {"app.py": BEFORE})
    assert run_check(RemovedFix(), repo) == []
    assert run_check(RemovedFix(), repo, Config(min_severity=3)) == []


DOCSTRING_BEFORE = '''\
def render():
    """Render safely.
    helper(option=True)
    """
    return 1
'''


def test_removed_fix_ignores_staged_python_docstring_changes(make_repo):
    repo = make_repo([
        ("Add render", {"app.py": "def render():\n    return 1\n"}),
        ("Fix crash in render (fixes #1)", {"app.py": DOCSTRING_BEFORE}),
    ])
    stage(repo, {"app.py": DOCSTRING_BEFORE.replace("option=True", "option=False")})
    assert run_check(RemovedFix(), repo) == []


def test_removed_fix_docstring_filter_keeps_executable_code(make_repo):
    fixed = "def render():\n    value = helper(option=True)\n    return value\n"
    repo = make_repo([
        ("Add render", {"app.py": "def render():\n    return 1\n"}),
        ("Fix crash in render (fixes #1)", {"app.py": fixed}),
    ])
    stage(repo, {"app.py": fixed.replace("option=True", "option=False")})
    assert len(run_check(RemovedFix(), repo)) == 1


def test_removed_fix_docstring_filter_deduplicates_after_suppressing(make_repo):
    fixed = '''\
def render():
    """Render safely.
    helper(documented=True)
    """
    value = helper(executable=True)
    return value
'''
    repo = make_repo([
        ("Add render", {"app.py": "def render():\n    return 1\n"}),
        ("Fix crash in render (fixes #1)", {"app.py": fixed}),
    ])
    changed = fixed.replace("documented=True", "documented=False").replace(
        "executable=True", "executable=False"
    )
    stage(repo, {"app.py": changed})
    (finding,) = run_check(RemovedFix(), repo)
    assert finding.line == 5  # the earlier docstring line doesn't consume this commit's finding


def test_removed_fix_docstring_filter_keeps_multiline_string_data(make_repo):
    fixed = 'QUERY = """\nSELECT helper(option=True)\n"""\n'
    repo = make_repo([
        ("Add query", {"app.py": 'QUERY = "select 1"\n'}),
        ("Fix query crash (fixes #1)", {"app.py": fixed}),
    ])
    stage(repo, {"app.py": fixed.replace("option=True", "option=False")})
    assert len(run_check(RemovedFix(), repo)) == 1


def test_removed_fix_docstring_parse_failure_is_conservative(make_repo):
    fixed = "def broken(\nvalue = helper(option=True)\n"
    repo = make_repo([
        ("Add broken file", {"app.py": "def broken(\n"}),
        ("Fix crash in broken file (fixes #1)", {"app.py": fixed}),
    ])
    stage(repo, {"app.py": fixed.replace("option=True", "option=False")})
    assert len(run_check(RemovedFix(), repo)) == 1


def test_removed_fix_docstring_read_failure_is_conservative(make_repo, monkeypatch):
    repo = make_repo([
        ("Add render", {"app.py": "def render():\n    return 1\n"}),
        ("Fix crash in render (fixes #1)", {"app.py": DOCSTRING_BEFORE}),
    ])
    stage(repo, {"app.py": DOCSTRING_BEFORE.replace("option=True", "option=False")})

    def fail_historical_read(repo, *args):
        raise subprocess.CalledProcessError(128, ["git", *args])

    monkeypatch.setattr(removed_fix_module, "run_git", fail_historical_read)
    assert len(run_check(RemovedFix(), repo)) == 1


def test_removed_fix_docstring_filter_does_not_read_non_python_files(make_repo, monkeypatch):
    fixed = "value = helper(option=True)\n"
    repo = make_repo([
        ("Add script", {"app.js": "value = 1\n"}),
        ("Fix script crash (fixes #1)", {"app.js": fixed}),
    ])
    stage(repo, {"app.js": fixed.replace("option=True", "option=False")})
    monkeypatch.setattr(
        removed_fix_module, "run_git",
        lambda *args: pytest.fail("non-Python files must not trigger a historical file read"),
    )
    assert len(run_check(RemovedFix(), repo)) == 1


def test_removed_fix_reads_docstrings_once_per_historical_file(make_repo, monkeypatch):
    fixed = DOCSTRING_BEFORE.replace(
        "    helper(option=True)\n",
        "    helper(option=True)\n    helper(other=True)\n",
    )
    repo = make_repo([
        ("Add render", {"app.py": "def render():\n    return 1\n"}),
        ("Fix crash in render (fixes #1)", {"app.py": fixed}),
    ])
    changed = fixed.replace("option=True", "option=False").replace("other=True", "other=False")
    stage(repo, {"app.py": changed})
    build_index(str(repo))
    store = Store.open(str(repo))
    changes = read_changes(str(repo))
    original = removed_fix_module.run_git
    calls = 0

    def count_historical_reads(repo, *args):
        nonlocal calls
        calls += 1
        return original(repo, *args)

    monkeypatch.setattr(removed_fix_module, "run_git", count_historical_reads)
    try:
        assert RemovedFix().run([*changes, *changes], store, Config()) == []
    finally:
        store.close()
    assert calls == 1


def test_removed_fix_does_not_read_file_without_an_eligible_fix(make_repo, monkeypatch):
    added = "def render():\n    value = helper(option=True)\n    return value\n"
    repo = make_repo([
        ("Add render", {"app.py": "def render():\n    return 1\n"}),
        ("Add optional rendering", {"app.py": added}),
    ])
    stage(repo, {"app.py": added.replace("option=True", "option=False")})
    monkeypatch.setattr(
        removed_fix_module, "run_git",
        lambda *args: pytest.fail("a non-fix trace must not trigger a historical file read"),
    )
    assert run_check(RemovedFix(), repo) == []


# --- readded-bug ------------------------------------------------------------------------


def test_readded_bug_warns_when_removed_line_comes_back(make_repo):
    repo = make_repo(FIXED)
    stage(repo, {"app.py": BEFORE + "    # back to the simple version\n"})
    findings = run_check(ReaddedBug(), repo)
    assert [(f.check, f.path, f.line) for f in findings] == [("readded-bug", "app.py", 3)]
    assert findings[0].evidence[0].startswith(sha(repo)[:7])


def test_readded_bug_quiet_when_line_still_exists_at_head(make_repo):
    old_line = "        self.view = lambda: self.send()\n"
    repo = make_repo(FIXED + [("Add second view", {"app.py": AFTER + "    def other(self):\n" + old_line})])
    # the fix removed one copy, but the same line lives on elsewhere in the file
    stage(repo, {"app.py": AFTER + old_line + "    def other(self):\n" + old_line})
    assert run_check(ReaddedBug(), repo) == []


def test_readded_bug_skips_trivial_lines(make_repo):
    repo = make_repo([
        ("Add app", {"app.py": "import os\nx = (\n    1,\n)\n"}),
        ("Fix crash on start (fixes #9)", {"app.py": "x = 1\n"}),
    ])
    stage(repo, {"app.py": "import os\nx = 1\n"})
    assert run_check(ReaddedBug(), repo) == []


# --- forgot-related-file ----------------------------------------------------------------


def paired_history(n=5):
    return [(f"Change {i}", {"model.py": f"m = {i}\n", "migration.py": f"v = {i}\n"}) for i in range(n)]


def test_cochange_warns_when_partner_not_staged(make_repo):
    repo = make_repo(paired_history())
    stage(repo, {"model.py": "m = 99\n"})
    (finding,) = run_check(ForgotRelatedFile(), repo)
    assert (finding.check, finding.path, finding.line) == ("forgot-related-file", "model.py", None)
    assert "migration.py" in finding.message and "100%" in finding.message


def test_cochange_quiet_when_partner_staged_ignored_or_rare(make_repo):
    repo = make_repo(paired_history())
    stage(repo, {"model.py": "m = 99\n", "migration.py": "v = 99\n"})
    assert run_check(ForgotRelatedFile(), repo) == []

    subprocess.run(["git", "-C", str(repo), "reset", "-q"], check=True)
    stage(repo, {"model.py": "m = 99\n"})
    assert run_check(ForgotRelatedFile(), repo, Config(ignore_paths=["migr*"])) == []
    assert run_check(ForgotRelatedFile(), repo, Config(cochange_min_support=6)) == []


def test_cochange_quiet_when_partner_deleted(make_repo):
    repo = make_repo(paired_history())
    subprocess.run(["git", "-C", str(repo), "rm", "-q", "migration.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "Drop migrations"], check=True)
    stage(repo, {"model.py": "m = 99\n"})
    assert run_check(ForgotRelatedFile(), repo) == []


def test_cochange_skips_variants_of_the_same_file():
    from blot.checks.cochange import is_variant

    assert is_variant("shared/ReactFeatureFlags.js", "shared/forks/ReactFeatureFlags.native-fb.js")
    assert is_variant("dom-esm/server/Node.js", "dom-webpack/server/Node.js")
    assert is_variant("validate/eslintrc.rn.js", "validate/eslintrc.fb.js")
    assert not is_variant("src/foo.c", "include/foo.h")  # source and header still pair
    assert not is_variant("model.py", "migration.py")


def test_removed_fix_quiet_when_the_whole_file_is_deleted(make_repo):
    repo = make_repo(FIXED)
    subprocess.run(["git", "-C", str(repo), "rm", "-q", "app.py"], check=True)
    assert run_check(RemovedFix(), repo) == []
