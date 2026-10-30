import json
import subprocess

from blot.bench import apply_labels, format_cases, format_replay, load_labels, replay, run_cases, save_labels
from blot.checks.readded_bug import ReaddedBug
from blot.checks.removed_fix import RemovedFix
from blot.config import Config
from blot.diff import is_meaningful_line, normalize_line

from test_checks import AFTER, BEFORE, GUARD, run_check, stage

HISTORY = [
    ("Add app", {"app.py": BEFORE}),
    ("Break reference cycle in App (fixes #3761)", {"app.py": AFTER}),
    ("Simplify app", {"app.py": AFTER.replace(GUARD, "self_ref = self")}),  # undoes the fix
]


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def test_replay_flags_the_commit_that_undid_a_fix(make_repo):
    repo = make_repo(HISTORY)
    result = replay(str(repo), 10, Config())
    assert result.checked == 2  # the root commit has no parent to compare against
    flagged = {c.subject: c.findings for c in result.commits}
    assert flagged["Simplify app"][0]["check"] == "removed-fix"
    assert "Break reference cycle" in flagged["Simplify app"][0]["evidence"][0]
    assert flagged["Break reference cycle in App (fixes #3761)"] == []
    assert "warnings per 10 commits: 5.00" in format_replay(result)


def test_replay_labels_give_precision_and_survive_line_drift(make_repo, tmp_path):
    repo = make_repo(HISTORY)
    result = replay(str(repo), 10, Config())
    path = str(tmp_path / "labels.json")

    labels = load_labels(path)
    new = apply_labels(result, labels)
    assert [(n["check"], n["verdict"]) for n in new] == [("removed-fix", None)]
    assert "precision: 0 useful / 0 labelled (0%), unlabelled: 1" in format_replay(result)
    assert "[?]" in format_replay(result)

    save_labels(path, labels, result, new)
    saved = load_labels(path)
    assert saved["head"] == result.head
    saved["labels"][0].update(verdict="useful", line=99)  # line numbers may drift between versions

    result = replay(str(repo), 10, Config())
    assert apply_labels(result, saved) == []  # nothing new to label
    out = format_replay(result)
    assert "precision: 1 useful / 1 labelled (100%), unlabelled: 0" in out and "[useful]" in out


def test_replay_does_not_see_the_future(make_repo):
    # "Add app" introduces a line that a later fix removes. Replaying "Add app" must not
    # call it a re-added bug: at that point in history the fix didn't exist yet.
    repo = make_repo([("Init", {"README.md": "x\n"}), *HISTORY[:2]])
    result = replay(str(repo), 10, Config())
    assert all(not c.findings for c in result.commits)


def test_cases_report_hits_and_quiet_cases(make_repo, tmp_path):
    repo = make_repo(HISTORY[:2])
    cases = [
        {"id": "warn", "repo": str(repo), "at": git(repo, "rev-parse", "HEAD"), "file": "app.py",
         "replace": [GUARD, "self_ref = self"], "expect": {"check": "removed-fix", "warn": True,
                                                            "commit": git(repo, "rev-parse", "HEAD")}},
        {"id": "quiet", "repo": str(repo), "at": git(repo, "rev-parse", "HEAD"), "file": "app.py",
         "replace": ["class App:", "class Application:"], "expect": {"check": "removed-fix", "warn": False}},
        {"id": "missing", "repo": str(repo), "at": git(repo, "rev-parse", "HEAD"), "file": "app.py",
         "replace": ["not in the file", "x"], "expect": {"check": "removed-fix", "warn": True, "commit": "abc"}},
        # a quiet case whose edit can't be made proves nothing, so it fails too
        {"id": "quiet-missing", "repo": str(repo), "at": git(repo, "rev-parse", "HEAD"), "file": "app.py",
         "replace": ["not in the file", "x"], "expect": {"check": "removed-fix", "warn": False}},
    ]
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(cases), encoding="utf-8")
    results = run_cases(str(path), str(tmp_path / "work"), Config())
    assert [r.correct for r in results] == [True, True, False, False]
    assert "found 0 times" in results[2].detail
    report = format_cases(results)
    assert "2/4 cases correct" in report and "precision 100%, recall 50%" in report


def test_tests_and_docs_are_skipped_unless_opted_in(make_repo):
    repo = make_repo([
        ("Add test", {"tests/test_app.py": BEFORE}),
        ("Break reference cycle in App (fixes #3761)", {"tests/test_app.py": AFTER}),
    ])
    stage(repo, {"tests/test_app.py": AFTER.replace(GUARD, "self_ref = self")})
    assert run_check(RemovedFix(), repo) == []
    assert len(run_check(RemovedFix(), repo, Config(check_tests=True))) == 1


def test_moved_lines_are_not_removals_or_readds(make_repo):
    repo = make_repo([("Add app", {"app.py": BEFORE}), ("Break reference cycle in App (fixes #3761)", {"app.py": AFTER})])
    # the guarded line moves to another file unchanged: not a removed fix
    (repo / "app.py").unlink()
    stage(repo, {"core.py": AFTER})
    assert run_check(RemovedFix(), repo) == []
    assert run_check(ReaddedBug(), repo) == []


def test_reformatted_or_annotated_lines_count_as_kept():
    from blot.diff import KeptLines

    kept = KeptLines([
        "    def links(self) -> dict[str, dict[str, str]]:",  # type hint added
        "raise InvalidProxyURL(",  # black re-wrap + quote change
        '    "Please check proxy URL. It is malformed"',
        ")",
        "    self_ref = self",
    ])
    assert kept.keeps("def links(self):")
    assert kept.keeps("raise InvalidProxyURL('Please check proxy URL. It is malformed')")
    assert not kept.keeps("self_ref = weakref.ref(self)")  # a real removal still counts

    # requests 561e4b68: a type hint that inserts `| None` is still cosmetic
    assert KeptLines(["def __init__(self, data: dict | None = None, **kwargs: Any) -> None:"]).keeps(
        "def __init__(self, data=None, **kwargs):")

    # Flask cabda59: same tokens plus an inserted `not` is a logic change, not a reformat
    edited = KeptLines(["if state.subdomain is not None and bp_subdomain is not None:"])
    assert not edited.keeps("if state.subdomain is not None and bp_subdomain is None:")


def test_meaningful_lines():
    for line in ("self_ref = weakref.ref(self)", "if state.subdomain is not None and bp_subdomain is None:",
                 'throw new Error("bad request body")'):
        assert is_meaningful_line(normalize_line(line)), line
    for line in ("return", "from functools import partial", "# Use a weakref to avoid a cycle",
                 ":param kwargs: Extra arguments passed to json.dumps.",
                 "then pushed when the function is called.  The current session is also",
                 "The `max_geom_collections` keyword limits how many nested (WKT) or",
                 "data. This prevents `flask.session` pointing to an out-of-date object."):
        assert not is_meaningful_line(normalize_line(line)), line
