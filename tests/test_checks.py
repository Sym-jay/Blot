import subprocess

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
