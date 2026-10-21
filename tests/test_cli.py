import json
import subprocess

from blot.checks import REGISTRY
from blot.cli import main
from blot.report import render
from blot.checks.base import Finding


def test_registry_has_three_checks():
    assert [c.id for c in REGISTRY] == ["removed-fix", "readded-bug", "forgot-related-file"]


def test_check_without_index_never_blocks(make_repo, capsys):
    repo = make_repo([("feat: init", {"a.py": "x = 1\n"})])
    assert main(["--repo", str(repo), "check"]) == 0
    assert "run `blot index`" in capsys.readouterr().err


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
