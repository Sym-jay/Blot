import subprocess

import pytest

from blot.checks.base import describe_commit
from blot.classify import adjust_for_labels
from blot.cli import main
from blot.github import enrich, github_repo
from blot.index import build_index
from blot.store import Store
from blot.why import WhyError, explain, parse_location, render

from test_checks import AFTER, BEFORE, GUARD

SPLIT = "Split the App into parts"  # a refactor commit that only moves the guarded line


def history():
    return [
        ("Add app", {"app.py": BEFORE}),
        ("Break reference cycle in App (fixes #3761)", {"app.py": AFTER}),
        (SPLIT, {"app.py": "from core import App\n", "core.py": AFTER}),
    ]


def indexed(repo):
    build_index(str(repo))
    return Store.open(str(repo))


# --- blot why ---------------------------------------------------------------------------


def test_why_traces_past_a_refactor(make_repo):
    repo = make_repo(history())
    store = indexed(repo)
    line = AFTER.splitlines().index(f"        {GUARD}") + 1
    path, explained = explain(store, f"core.py:{line}")
    assert path == "core.py"
    (e,) = explained
    assert store.get_commit(e.sha).subject.startswith("Break reference cycle")
    text = render(store, path, explained)
    assert "explained by" in text and "[bug fix]" in text
    assert f"core.py:{line}" in text


def test_why_cli_and_errors(make_repo, capsys):
    repo = make_repo(history())
    assert main(["--repo", str(repo), "why", "core.py:1"]) == 0  # builds the index itself
    assert "first run, indexing" in capsys.readouterr().err
    assert main(["--repo", str(repo), "why", "missing.py:1"]) == 1
    assert "not tracked" in capsys.readouterr().err
    assert main(["--repo", str(repo), "why", "core.py:999"]) == 1
    assert "only" in capsys.readouterr().err
    assert main(["--repo", str(repo), "why", "core.py:1-3"]) == 0


@pytest.mark.parametrize("bad", ["core.py", "core.py:0", "core.py:5-2", ":3"])
def test_parse_location_rejects(bad):
    with pytest.raises(WhyError):
        parse_location(bad)


# --- blot index --github ----------------------------------------------------------------


class FakeGitHub:
    def __init__(self, issues: dict[int, dict | None], issues_enabled: bool = True):
        self.issues, self.issues_enabled, self.queries = issues, issues_enabled, []

    def __call__(self, query: str) -> dict:
        self.queries.append(query)
        repo = {"hasIssuesEnabled": self.issues_enabled}
        for n, node in self.issues.items():
            if f"n{n}:" in query:
                repo[f"n{n}"] = node
        return {"data": {"repository": repo}}


def with_remote(repo):
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/acme/app.git"], check=True)
    return repo


def test_github_repo_from_remote(make_repo):
    repo = make_repo(history())
    assert github_repo(str(repo)) is None
    with_remote(repo)
    assert github_repo(str(repo)) == ("acme", "app")


def test_enrich_stores_titles_and_skips_known_refs(make_repo):
    repo = with_remote(make_repo(history()))
    store = indexed(repo)
    fake = FakeGitHub({3761: {"__typename": "Issue", "title": "Memory leak in app", "labels": {"nodes": [{"name": "bug"}]}}})
    result = enrich(str(repo), store, runner=fake)
    assert (result.status, result.fetched, result.missing) == ("ok", 1, 0)
    assert store.describe_issue("#3761") == '#3761 "Memory leak in app"'
    fix = next(r for r in store.commits_with_issues() if r.issues == ["#3761"])
    assert '#3761 "Memory leak in app"' in describe_commit(fix, store)

    again = enrich(str(repo), store, runner=fake)  # nothing new to fetch
    assert again.fetched == 0 and len(fake.queries) == 1


def test_enrich_labels_demote_a_feature_that_closed_an_issue(make_repo):
    repo = with_remote(make_repo([
        ("Add app", {"app.py": BEFORE}),
        ("Handle reference handling (fixes #12)", {"app.py": AFTER}),  # looks like a fix by its message
    ]))
    store = indexed(repo)
    row = store.commits_with_issues()[0]
    assert row.severity == 2
    fake = FakeGitHub({12: {"__typename": "Issue", "title": "Support weak references", "labels": {"nodes": [{"name": "enhancement"}]}}})
    result = enrich(str(repo), store, runner=fake)
    assert result.demoted == 1 and store.get_commit(row.sha).severity == 1


def test_enrich_skips_repos_with_issues_disabled(make_repo):
    repo = with_remote(make_repo(history()))
    store = indexed(repo)
    result = enrich(str(repo), store, runner=FakeGitHub({3761: None}, issues_enabled=False))
    assert "disabled" in result.status
    assert store.known_issue_refs() == set()


def test_enrich_without_github_remote(make_repo):
    repo = make_repo(history())
    assert "no GitHub remote" in enrich(str(repo), indexed(repo), runner=FakeGitHub({})).status


@pytest.mark.parametrize(
    "subject, severity, labels, expected",
    [
        ("Tweak parser (#5)", 1, ["bug"], 2),
        ("Tweak parser (#5)", 1, ["Type: Bug", "core"], 2),
        ("Handle refs (fixes #5)", 2, ["enhancement"], 1),
        ("Handle refs (fixes #5)", 2, ["enhancement", "blueprints"], 2),  # one label isn't "not a bug"
        ("Fix crash in refs (fixes #5)", 2, ["enhancement"], 2),  # a named defect beats the label
        ("Handle refs (fixes #5)", 2, [], 2),
    ],
)
def test_adjust_for_labels(subject, severity, labels, expected):
    assert adjust_for_labels(subject, severity, labels) == expected
