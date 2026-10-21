import os
import subprocess

from blot.git import git_dir, run_git


def test_git_dir_matches_git_from_root_subdir_and_worktree(make_repo, tmp_path, monkeypatch):
    monkeypatch.delenv("GIT_DIR", raising=False)
    repo = make_repo([("init", {"pkg/a.py": "x = 1\n"})])
    expected = os.path.normcase(os.path.normpath(run_git(str(repo), "rev-parse", "--absolute-git-dir").strip()))

    assert os.path.normcase(git_dir(str(repo))) == expected
    assert os.path.normcase(git_dir(str(repo / "pkg"))) == expected

    wt = tmp_path / "wt"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", str(wt)], check=True)
    wt_expected = os.path.normcase(os.path.normpath(run_git(str(wt), "rev-parse", "--absolute-git-dir").strip()))
    assert os.path.normcase(git_dir(str(wt))) == wt_expected
