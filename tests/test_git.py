import os
import subprocess

from blot.git import git_dir, head_sha, run_git


def test_head_sha_matches_git_in_every_ref_layout(make_repo, tmp_path, monkeypatch):
    monkeypatch.delenv("GIT_DIR", raising=False)
    repo = make_repo([("one", {"a.py": "x = 1\n"}), ("two", {"a.py": "x = 2\n"})])
    rev = lambda path: run_git(str(path), "rev-parse", "HEAD").strip()  # noqa: E731

    assert head_sha(str(repo)) == rev(repo)  # loose ref
    run_git(str(repo), "pack-refs", "--all")
    assert head_sha(str(repo)) == rev(repo)  # packed-refs only
    wt = tmp_path / "wt"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "side", str(wt)], check=True)
    assert head_sha(str(wt)) == rev(wt)  # worktree: ref lives in the common dir
    run_git(str(repo), "checkout", "-q", "--detach", "HEAD~1")
    assert head_sha(str(repo)) == rev(repo)  # detached HEAD
    empty = tmp_path / "empty"
    subprocess.run(["git", "init", "-q", str(empty)], check=True)
    assert head_sha(str(empty)) is None  # no commits yet


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
