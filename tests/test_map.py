import json

import pytest

from blot.cli import main
from blot.fixmap import build_map, render, to_json
from blot.index import build_index
from blot.store import Store
from blot.why import WhyError

from test_checks import AFTER, GUARD
from test_why_github import SPLIT, history

GUARD_LINE = AFTER.splitlines().index(f"        {GUARD}") + 1
FIX_LINES = [GUARD_LINE, GUARD_LINE + 1]  # the fix added the guard and changed the line after it


def indexed(repo):
    build_index(str(repo))
    return Store.open(str(repo))


def test_map_marks_the_fix_line(make_repo):
    repo = make_repo(history()[:2])  # before the split: app.py holds the fix
    store = indexed(repo)
    fmap = build_map(store, "app.py")
    (span,) = fmap.spans
    assert (span.start, span.end, span.lines) == (GUARD_LINE, GUARD_LINE + 1, FIX_LINES)
    assert store.get_commit(span.sha).subject.startswith("Break reference cycle")
    assert span.via == []
    text = render(store, fmap)
    assert f"{GUARD_LINE}-{GUARD_LINE + 1}  | {GUARD}" in text
    assert '"Break reference cycle in App (fixes #3761)"' in text and "fixes #3761)" in text


def test_map_traces_past_a_refactor(make_repo):
    repo = make_repo(history())  # the split moved the fix line into core.py
    store = indexed(repo)
    fmap = build_map(store, "core.py")
    (span,) = fmap.spans
    assert span.lines == FIX_LINES
    assert store.get_commit(span.sha).subject.startswith("Break reference cycle")
    assert [store.get_commit(v).subject for v in span.via] == [SPLIT]
    assert f'traced past refactor {span.via[0][:7]} "{SPLIT}"' in render(store, fmap)
    data = to_json(store, fmap)
    (r,) = data["ranges"]
    assert (r["start"], r["end"], r["commit"], r["via"]) == (GUARD_LINE, GUARD_LINE + 1, span.sha, span.via)
    assert data["commits"][span.sha]["issues"] == [{"ref": "#3761", "title": None}]
    assert data["commits"][span.via[0]]["kind"] == "refactor"
    # the file that only imports the class now has nothing load-bearing
    assert build_map(store, "app.py").spans == []


def test_map_groups_lines_from_one_fix_across_blank_lines(make_repo):
    fix = "def load(path):\n    if path is None:\n\n        raise ValueError('path is required')\n    return open(path)\n"
    repo = make_repo([
        ("Add loader", {"load.py": "def load(path):\n    return open(path)\n"}),
        ("Fix crash when path is missing (fixes #7)", {"load.py": fix}),
    ])
    store = indexed(repo)
    (span,) = build_map(store, "load.py").spans
    assert (span.start, span.end, span.lines) == (2, 4, [2, 4])


def test_map_skips_docstrings_and_respects_min_severity(make_repo):
    repo = make_repo([
        ("Add render", {"r.py": "def render():\n    return 1\n"}),
        ("Fix crash in render (fixes #1)", {"r.py": 'def render():\n    """Render safely, helper(option=True)."""\n    return 1\n'}),
        ("fix typo", {"r.py": 'def render():\n    """Render safely, helper(option=True)."""\n    return compute(1)\n'}),
    ])
    store = indexed(repo)
    assert build_map(store, "r.py").spans == []  # the bug fix only added a docstring
    (span,) = build_map(store, "r.py", min_severity=1).spans  # the routine fix changed the return
    assert span.lines == [3]


def test_map_cli(make_repo, capsys):
    repo = make_repo(history())
    assert main(["--repo", str(repo), "map", "core.py"]) == 0  # builds the index itself
    out = capsys.readouterr().out
    assert out.startswith("core.py: 2 load-bearing lines in 1 range, from 1 fix")
    assert main(["--repo", str(repo), "map", "core.py", "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["path"] == "core.py" and len(data["ranges"]) == 1
    assert main(["--repo", str(repo), "map", "core.py", "--full"]) == 0
    full = capsys.readouterr().out
    assert f"{GUARD_LINE} {data['ranges'][0]['commit'][:7]} | " in full
    assert main(["--repo", str(repo), "map", "core.py", "--rev", "HEAD~1"]) == 1  # no core.py yet
    assert "doesn't exist at HEAD~1" in capsys.readouterr().err
    assert main(["--repo", str(repo), "map", "app.py", "--rev", "HEAD~1"]) == 0
    assert "app.py at HEAD~1: 2 load-bearing lines" in capsys.readouterr().out
    assert main(["--repo", str(repo), "map", "missing.py"]) == 1
    assert "not tracked" in capsys.readouterr().err


def test_map_rejects_untracked_file(make_repo):
    repo = make_repo(history())
    (repo / "new.py").write_text("x = 1\n", encoding="utf-8")
    store = indexed(repo)
    with pytest.raises(WhyError, match="not tracked"):
        build_map(store, "new.py")
