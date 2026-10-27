import json
import subprocess

from blot.checks import REGISTRY
from blot.cli import main
from blot.report import render
from blot.checks.base import Finding


def test_registry_has_three_checks():
    assert [c.id for c in REGISTRY] == ["removed-fix", "readded-bug", "forgot-related-file"]


def test_check_builds_the_index_on_first_run_then_refreshes_it(make_repo, capsys):
    import subprocess

    from test_checks import AFTER, BEFORE, GUARD

    repo = make_repo([("feat: init", {"a.py": "x = 1\n"})])
    assert main(["--repo", str(repo), "check"]) == 0
    assert "first run, indexing" in capsys.readouterr().err
    assert main(["--repo", str(repo), "check"]) == 0
    assert capsys.readouterr().err == ""  # already indexed: silent

    # a fix committed after the first check must be known to the next one (incremental refresh)
    for message, content in (("Add app", BEFORE), ("Break reference cycle in App (fixes #3761)", AFTER)):
        (repo / "app.py").write_text(content, encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", message], check=True)
    (repo / "app.py").write_text(AFTER.replace(GUARD, "self_ref = self"), encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    assert main(["--repo", str(repo), "check"]) == 0
    assert "removed-fix" in capsys.readouterr().out


def test_render_text():
    out = render([Finding("removed-fix", "app.py", 355, "You're changing a line added by a bug fix.", ['8efea0cc "Break reference cycle"'])])
    assert "1 warning" in out and "app.py:355" in out and "8efea0cc" in out


GUARD = "        self_ref = weakref.ref(self)\n"
APP = """\
import weakref


class App:
    def __init__(self, name):
        self.name = name
        self.config = {}
        self.view = lambda: self_ref().send()

    def send(self):
        return self.name
"""


def test_removed_fix_traces_past_refactor(make_repo, capsys):
    """The Flask case in miniature: fix commit -> file split commit -> edit removes the fix."""
    fixed = APP.replace("        self.view", GUARD + "        self.view")
    repo = make_repo([
        ("Add app", {"app.py": APP.replace("self_ref()", "self")}),
        ("Break reference cycle in App (fixes #3761)", {"app.py": fixed}),
        ("Split the App into sansio and IO parts", {
            "sansio/app.py": fixed.replace("class App:", "class BaseApp:"),
            "app.py": "from sansio.app import BaseApp\n\n\nclass App(BaseApp):\n    pass\n",
        }),
    ])
    sha = lambda rev: subprocess.run(["git", "-C", str(repo), "rev-parse", rev], capture_output=True, text=True).stdout.strip()  # noqa: E731
    split_file = repo / "sansio" / "app.py"
    split_file.write_text(fixed.replace("class App:", "class BaseApp:").replace(GUARD, ""), encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)

    assert main(["--repo", str(repo), "index", "--quiet"]) == 0
    capsys.readouterr()
    assert main(["--repo", str(repo), "check", "--format", "json"]) == 0
    (finding,) = [f for f in json.loads(capsys.readouterr().out) if f["check"] == "removed-fix"]
    assert (finding["path"], finding["line"]) == ("sansio/app.py", 8)
    # plain blame stops at the split; blot must cite the fix behind it
    assert finding["evidence"][0].startswith(sha("HEAD~1")[:7] + ' "Break reference cycle in App (fixes #3761)"')
    assert finding["evidence"][1].startswith("traced past refactor " + sha("HEAD")[:7])


def test_check_range_reads_removed_lines_at_the_base(make_repo, capsys):
    """In CI the PR's commits are already in the index and HEAD no longer has the removed line."""
    from test_checks import AFTER, BEFORE, GUARD

    repo = make_repo([("Add app", {"app.py": BEFORE}),
                      ("Break reference cycle in App (fixes #3761)", {"app.py": AFTER})])
    git = lambda *args: subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)  # noqa: E731
    git("switch", "-q", "-c", "pr")
    (repo / "app.py").write_text(AFTER.replace(GUARD, "self_ref = self"), encoding="utf-8")
    git("commit", "-q", "-am", "Simplify App")
    git("switch", "-q", "main")
    (repo / "README.md").write_text("app\n", encoding="utf-8")
    git("add", "README.md")
    git("commit", "-q", "-m", "docs: readme")  # main moves on, so A..B and A...B differ
    git("switch", "-q", "pr")

    for rev_range in ("HEAD~1..HEAD", "main...pr"):
        assert main(["--repo", str(repo), "check", "--range", rev_range, "--format", "json"]) == 0
        found = [(f["check"], f["path"], f["line"]) for f in json.loads(capsys.readouterr().out)]
        assert ("removed-fix", "app.py", 3) in found, rev_range
