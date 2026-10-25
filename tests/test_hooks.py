import os

from blot import hooks


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_install_creates_hook_and_is_idempotent(make_repo):
    repo = str(make_repo([("init", {"a.py": "x = 1\n"})]))
    assert hooks.install(repo, python="/usr/bin/python3") == "installed"
    path = hooks.hook_path(repo)
    text = read(path)
    assert text.startswith("#!/bin/sh\n") and '"/usr/bin/python3" -m blot check' in text
    assert hooks.install(repo, python="/usr/bin/python3") == "already installed"
    assert read(path) == text


def test_install_appends_to_existing_hook_and_uninstall_restores_it(make_repo):
    repo = str(make_repo([("init", {"a.py": "x = 1\n"})]))
    path = hooks.hook_path(repo)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    original = "#!/bin/sh\nnpm run lint\n"
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(original)

    assert hooks.install(repo, python="C:\\Py\\python.exe") == "appended"
    text = read(path)
    assert text.startswith(original) and '"C:/Py/python.exe" -m blot check' in text

    assert hooks.uninstall(repo) == "removed"
    assert read(path) == original


def test_real_commit_runs_hook_and_is_not_blocked(make_repo):
    import subprocess

    repo = make_repo([("init", {"a.py": "x = 1\n"})])
    hooks.install(str(repo))  # this interpreter, so `-m blot` resolves
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "a.py"], check=True)
    done = subprocess.run(["git", "-C", str(repo), "commit", "-m", "change"], capture_output=True, text=True)
    assert done.returncode == 0
    assert "first run, indexing" in done.stderr  # the hook really ran, and indexed the repo


def test_uninstall_deletes_hook_blot_created(make_repo):
    repo = str(make_repo([("init", {"a.py": "x = 1\n"})]))
    hooks.install(repo, python="/usr/bin/python3")
    assert hooks.uninstall(repo) == "removed"
    assert not os.path.exists(hooks.hook_path(repo))
    assert hooks.uninstall(repo) == "not installed"
