"""bench/regressions.py: recall on real regressions (culprit commit named by a later fix)."""

import os
import subprocess
import sys

from blot.config import Config
from blot.index import _read_history, build_index
from blot.store import Store

from test_checks import AFTER, BEFORE, GUARD

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bench"))
import regressions  # noqa: E402


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def commit(repo, message, files):
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def test_find_references_reads_the_real_conventions():
    refs = regressions.find_references
    assert refs('Revert "x"\n\nThis reverts commit 35e64cf2bc4a980274ae0cb2a4bda11970560b7e.') == [
        ("revert", "35e64cf2bc4a980274ae0cb2a4bda11970560b7e")]
    assert refs("Fixed #36000 -- Fixed crash.\n\nRegression in bdbda29c3e126754c3ae04ceb5c5d35d49aae01c.") == [
        ("regression", "bdbda29c3e126754c3ae04ceb5c5d35d49aae01c")]
    assert refs("Regression introduced by dcdd219ee1, refs #25817.") == [("regression", "dcdd219ee1")]
    assert refs("Fixes: 1a2b3c4d5e") == [("fixes-trailer", "1a2b3c4d5e")]
    assert refs("Broken in 7785e03ba89.") == [("broken", "7785e03ba89")]
    assert refs("Reverts facebook/react#34654") == [("revert-pr", "#34654")]
    assert refs("Fix a regression from #36425 where entries") == [("regression-pr", "#36425")]
    # a feature mention and a test name are not culprits
    assert refs("CharField.strip was introduced in 11cac1bd8ef7546, and is True") == []
    assert refs("Added regression tests for deadbeef1.") == []


def test_ancestor_cochange_matches_an_index_built_at_that_commit(make_repo):
    history = [(f"change {i}", {"a.py": f"a = {i}\n", "b.py": f"b = {i}\n", **({"c.py": f"c = {i}\n"} if i % 2 else {})})
               for i in range(8)]
    repo = make_repo(history)
    parent = git(repo, "rev-parse", "HEAD~2")
    db = str(repo / "early.db")
    build_index(str(repo), full=True, upto=parent, db_path=db)
    store = Store.open(str(repo), db)
    every = [(info.sha, paths) for info, _, paths in _read_history(str(repo), "HEAD")]
    ours = regressions.AncestorCochange(every, set(git(repo, "rev-list", parent).split()), 30)
    for path in ("a.py", "b.py", "c.py"):
        for support, confidence in ((5, 0.8), (3, 0.5), (1, 0.1)):
            assert ours.partners(path, support, confidence) == store.partners(path, support, confidence)
    store.close()


def test_collect_and_run_find_the_reverted_culprit(make_repo):
    repo = make_repo([("Add app", {"app.py": BEFORE}), ("Break reference cycle in App (fixes #3761)", {"app.py": AFTER})])
    culprit = commit(repo, "Simplify app", {"app.py": AFTER.replace(GUARD, "self_ref = self")})
    fix = commit(repo, f'Revert "Simplify app"\n\nThis reverts commit {culprit}.', {"app.py": AFTER})
    tests_only = commit(repo, "Add tests", {"tests/test_app.py": "def test_app():\n    assert True\n"})
    commit(repo, f"Fix flaky test\n\nRegression in {tests_only}.", {"tests/test_app.py": "def test_app():\n    pass\n"})
    commit(repo, f'Revert "Revert "Simplify app""\n\nThis reverts commit {fix}.',
           {"app.py": AFTER.replace(GUARD, "self_ref = self")})

    data = regressions.collect(str(repo), Config())
    assert [(p["culprit"], p["fix"], p["kind"]) for p in data["pairs"]] == [(culprit[:10], fix[:10], "revert")]
    assert data["pairs"][0]["fix_files"] == ["app.py"]
    assert data["dropped"] == {"touches no checked file (tests, docs, generated)": 1,
                               "culprit is itself a revert (a re-land, not a bug)": 1}

    data = regressions.run(data, Config())
    pair = data["pairs"][0]
    assert [(f["check"], f["path"]) for f in pair["findings"]] == [("removed-fix", "app.py")]
    assert "Break reference cycle" in pair["findings"][0]["evidence"][0]
    assert pair["findings"][0]["near_fix"] == "fix-touched-hunk"  # the revert rewrote that very line
    assert [(o["path"], o["severity"]) for o in pair["removed_origins"]] == [("app.py", 2)]

    pair.update(bug=True, verdict="hit-on-target")
    s = regressions.summarize(data)
    assert (s["bugs"], s["warned"], s["on_target"], s["within_model"]) == (1, 1, 1, 1)
    assert "1/1 (100%)" in regressions.format_report([("demo", data)])


def test_fix_proximity_tells_the_warned_hunk_from_the_rest_of_the_file(make_repo):
    body = "".join(f"line_{i} = {i}\n" for i in range(1, 21))
    repo = make_repo([("Add app", {"app.py": body})])
    # the culprit deletes line 3 and rewrites line 15
    culprit = commit(repo, "Tidy", {"app.py": body.replace("line_3 = 3\n", "").replace("line_15 = 15", "line_15 = 0")})
    culprit_hunks = regressions.hunks(str(repo), culprit + "^", culprit)
    assert culprit_hunks["app.py"][1] == [(3, 1, 2, 0), (15, 1, 14, 1)]
    # the fix only touches the rewritten line
    after = (repo / "app.py").read_text(encoding="utf-8")
    fix = commit(repo, "Fix", {"app.py": after.replace("line_15 = 0", "line_15 = 15")})
    touched = regressions.culprit_lines_fix_touched(str(repo), culprit, fix)
    assert touched == {("app.py", 14)}
    near = lambda line: regressions.fix_proximity({"path": "app.py", "line": line}, culprit_hunks, touched)  # noqa: E731
    assert (near(15), near(3), near(None)) == ("fix-touched-hunk", "fix-touched-file", "fix-touched-file")
    # a fix that re-adds the deleted line lands between the culprit's lines 2 and 3
    fix2 = commit(repo, "Restore", {"app.py": body.replace("line_15 = 15", "line_15 = 15")})
    touched2 = regressions.culprit_lines_fix_touched(str(repo), culprit, fix2)
    assert regressions.fix_proximity({"path": "app.py", "line": 3}, culprit_hunks, touched2) == "fix-touched-hunk"


def test_merge_judgements_keeps_hand_verdicts():
    old = {"pairs": [{"culprit": "abc", "bug": True, "verdict": "miss-gap", "note": "classification"}]}
    new = {"pairs": [{"culprit": "abc"}, {"culprit": "def"}]}
    merged = regressions.merge_judgements(new, old)
    assert merged["pairs"][0]["verdict"] == "miss-gap" and "verdict" not in merged["pairs"][1]


def test_summary_leaves_out_reverts_that_were_not_bugs():
    data = {"pairs": [
        {"bug": True, "verdict": "miss-outside", "findings": []},
        {"bug": False, "verdict": "miss-outside", "findings": []},
        {"bug": True, "verdict": "hit-elsewhere", "findings": [{}]},
    ]}
    s = regressions.summarize(data)
    assert (s["pairs"], s["bugs"], s["not_a_bug"], s["warned"], s["within_model"]) == (3, 2, 1, 1, 1)
