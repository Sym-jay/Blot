import os
import subprocess

from blot.cli import main
from blot.diff import line_hash, normalize_line
from blot.index import _new_path, build_index
from blot.store import Store

GUARD = "self_ref = weakref.ref(self)"


def history():
    return [
        ("Add app", {"app.py": "class App:\n    def __init__(self):\n        self.view = lambda: self.send()\n", "app_test.py": "t = 1\n"}),
        ("Break reference cycle in App (fixes #3761)", {"app.py": f"class App:\n    def __init__(self):\n        {GUARD}\n        self.view = lambda: self_ref().send()\n", "app_test.py": "t = 2\n"}),
        ("Tweak app", {"app.py": f"class App:\n    def __init__(self):\n        {GUARD}\n        self.view = lambda: self_ref().send()\n        self.ok = True\n", "app_test.py": "t = 3\n"}),
    ]


def test_index_classifies_commits_and_records_fix_lines(make_repo):
    repo = make_repo(history())
    result = build_index(str(repo))
    assert (result.commits, result.fixes, result.full) == (3, 1, True)

    store = Store.open(str(repo))
    fix_sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD~1"], capture_output=True, text=True).stdout.strip()
    row = store.get_commit(fix_sha)
    assert row.severity == 2 and row.issues == ["#3761"]
    # the fix removed the old lambda line; re-adding it later should be detectable
    removed = normalize_line("        self.view = lambda: self.send()")
    assert store.fixes_that_removed(line_hash(removed)) == [("app.py", fix_sha)]


def test_index_mines_cochange(make_repo):
    repo = make_repo(history())
    build_index(str(repo))
    store = Store.open(str(repo))
    assert store.partners("app.py", min_support=3, min_confidence=0.8) == [("app_test.py", 3, 1.0)]
    assert store.partners("app.py", min_support=4, min_confidence=0.8) == []


def test_incremental_then_noop(make_repo):
    repo = make_repo(history())
    build_index(str(repo))
    (repo / "notes.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "Add notes"], check=True)

    second = build_index(str(repo))
    assert (second.commits, second.full) == (1, False)
    assert build_index(str(repo)).commits == 0
    assert Store.open(str(repo)).stats()["commits"] == 4


def test_rewritten_history_triggers_full_rebuild(make_repo):
    repo = make_repo(history())
    build_index(str(repo))
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "--amend", "-m", "Tweak app (amended)"], check=True)
    result = build_index(str(repo))
    assert result.full and result.commits == 3


def test_index_db_lives_in_git_dir(make_repo):
    repo = make_repo(history())
    assert main(["--repo", str(repo), "index", "--quiet"]) == 0
    assert os.path.exists(repo / ".git" / "blot.db")


def test_parallel_history_read_matches_serial(make_repo, tmp_path, monkeypatch):
    import blot.index as ix

    repo = make_repo(history())
    build_index(str(repo), db_path=str(tmp_path / "s.db"))
    monkeypatch.setattr(ix, "_PARALLEL_FROM", 1)  # force slicing even for 3 commits
    monkeypatch.setattr(ix, "_workers", lambda: 3)  # 6 slices for 3 commits: some would be empty
    build_index(str(repo), db_path=str(tmp_path / "p.db"))
    a, b = Store.open(str(repo), str(tmp_path / "s.db")), Store.open(str(repo), str(tmp_path / "p.db"))
    for table in ("commits", "fix_removed_lines", "file_commits", "cochange"):
        rows = lambda s: sorted(s.conn.execute(f"SELECT * FROM {table}").fetchall())  # noqa: E731
        assert rows(a) == rows(b), table


def test_empty_repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert build_index(str(tmp_path)).commits == 0


def test_rename_paths():
    assert _new_path("src/{old => new}/app.py") == ("src/new/app.py", True)
    assert _new_path("a.py => b.py") == ("b.py", True)
    assert _new_path("{ => sub}/x.py") == ("sub/x.py", True)
    assert _new_path("plain.py") == ("plain.py", False)
