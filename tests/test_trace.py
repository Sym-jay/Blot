"""Where tracing must stop. See bench/RESULTS.md, "Tried and dropped: tracing past unchanged lines"."""

import pytest

import blot.trace as trace_module
from blot.checks.removed_fix import RemovedFix
from blot.fixmap import build_map
from blot.git import run_git
from blot.index import build_index
from blot.store import Store
from blot.trace import RefactorOrigins, Trail, trace_lines
from blot.why import explain

from test_checks import run_check, stage

FIX = "Copy current session object in copy() (fixes #2935)"
MERGE = "merge app and request context"


def indexed(repo):
    build_index(str(repo))
    return Store.open(str(repo))


def ctx(session_attr: str, extra: str = "") -> str:
    return (
        "class RequestContext:\n"
        "    def copy(self):\n"
        "        return self.__class__(\n"
        "            self.app,\n"
        f"            session=self.{session_attr},\n"
        "        )\n" + extra
    )


def test_a_rewritten_line_stops_the_trace(make_repo):
    # Flask c2705ffd: `session=self.session,` became `session=self._session,` in a big non-refactor
    # commit. The fix's intent survives, but its tokens don't, so `why` names the rewrite.
    repo = make_repo([
        ("Add contexts", {"ctx.py": "class RequestContext:\n    def copy(self):\n        return self.__class__(\n            self.app,\n        )\n"}),
        (FIX, {"ctx.py": ctx("session")}),
        (MERGE, {"ctx.py": ctx("_session", "\n    @property\n    def session(self):\n        return self._session\n")}),
    ])
    store = indexed(repo)
    _, explained = explain(store, "ctx.py:5")
    (e,) = explained
    assert store.get_commit(e.sha).subject == MERGE
    assert e.via == []


def test_a_fix_that_wraps_a_value_in_a_call_keeps_the_line(make_repo):
    # Express 76eaa32 changed `this.set('Location', loc)` to `this.set('Location', encodeUrl(loc))`.
    # Token-wise the old line survives inside the new one, but the fix is why the line exists:
    # tracing must not hop past it, or the warning on the later open-redirect fix disappears.
    before = "function location(loc) {\n  return this.set('Location', loc);\n}\n"
    after = "function location(loc) {\n  return this.set('Location', encodeUrl(loc));\n}\n"
    repo = make_repo([
        ("Add location", {"response.js": before}),
        ("Encode URL in res.location (fixes #2897)", {"response.js": after}),
    ])
    store = indexed(repo)
    _, explained = explain(store, "response.js:2")
    (e,) = explained
    assert store.get_commit(e.sha).subject.startswith("Encode URL")
    stage(repo, {"response.js": before})
    (finding,) = run_check(RemovedFix(), repo)
    assert "Encode URL" in finding.evidence[0]


def test_traces_black_keyword_comma_to_the_original_fix(make_repo):
    # The opener reflows as in Flask, but the final keyword and closing delimiter stay on
    # their own lines. The later edit changes only the session argument.
    original = "def copy(self):\n    return make(self.app,\n        session=self.session\n    )\n"
    formatted = "def copy(self):\n    return make(\n        self.app,\n        session=self.session,\n    )\n"
    repo = make_repo([
        ("Add contexts", {"ctx.py": "def copy(self):\n    return make(self.app)\n"}),
        (FIX, {"ctx.py": original}),
        ("Reformat with black", {"ctx.py": formatted}),
    ])
    store = indexed(repo)
    fix = run_git(str(repo), "rev-parse", "HEAD^").strip()
    black = run_git(str(repo), "rev-parse", "HEAD").strip()

    assert trace_lines(store, "HEAD", "ctx.py", {4: formatted.splitlines()[3]}) == {
        4: Trail(fix, "ctx.py", 3, [black]),
    }
    _, (explained,) = explain(store, "ctx.py:4")
    assert (explained.sha, explained.via) == (fix, [black])
    (span,) = [span for span in build_map(store, "ctx.py").spans if span.start <= 4 <= span.end]
    assert (span.sha, span.via) == (fix, [black])

    stage(repo, {"ctx.py": formatted.replace("session=self.session", "session=self._session")})
    (finding,) = run_check(RemovedFix(), repo)
    assert (finding.path, finding.line) == ("ctx.py", 4)
    assert finding.evidence[0].startswith(fix[:7])
    assert black[:7] in finding.evidence[1]


ARG = "session=self.session"


def call(argument=ARG, callee="make"):
    return f"{callee}(\n    {argument}\n)\n"


def test_carries_historical_text_to_the_next_hop(make_repo):
    original = call()
    # Git's copy threshold does not credit the argument to the old file on its own.
    repo = make_repo([
        ("Add contexts", {"old.py": "make()\n"}),
        (FIX, {"old.py": original}),
        ("Split out context", {"old.py": "", "ctx.py": original}),
        ("Reformat with black", {"ctx.py": call(ARG + ",")}),
    ])
    store = indexed(repo)
    fix, split, black = [run_git(str(repo), "rev-parse", rev).strip() for rev in ("HEAD^^", "HEAD^", "HEAD")]
    assert trace_lines(store, "HEAD", "ctx.py", {2: ARG + ","}) == {
        2: Trail(fix, "old.py", 2, [black, split]),
    }


def test_traces_removed_comma_after_checking_string_tokens(make_repo):
    arg = 'label="a,b"'
    prefix = 'separator = "a\u2028b"\n'
    repo = make_repo([
        ("Add contexts", {"ctx.py": prefix + "make()\n"}),
        (FIX, {"ctx.py": prefix + call(arg + ",", "créate")}),
        ("Reformat with black", {"ctx.py": prefix + call(arg, "créate")}),
    ])
    store = indexed(repo)
    fix, black = [run_git(str(repo), "rev-parse", rev).strip() for rev in ("HEAD^", "HEAD")]
    assert trace_lines(store, "HEAD", "ctx.py", {3: arg}) == {3: Trail(fix, "ctx.py", 3, [black])}


def test_identical_call_tokens_do_not_establish_an_origin(make_repo):
    # The first call belongs to a feature. The second call supplies the only
    # comma-free text match and has the same tokens as the newly formatted call.
    before = call("k=(v)") + call("k=v")
    after = call("k=v,") + call("k = v")
    repo = make_repo([
        ("Add calls", {"ctx.py": "make()\nmake()\n"}),
        ("Fix crash when k is unset (fixes #1)", {"ctx.py": "make()\n" + call("k=v")}),
        ("Add k to the first call", {"ctx.py": before}),
        ("Reformat with black", {"ctx.py": after}),
    ])
    store = indexed(repo)
    fix, black = [run_git(str(repo), "rev-parse", rev).strip() for rev in ("HEAD^^", "HEAD")]
    assert trace_lines(store, "HEAD^", "ctx.py", {5: "    k=v"}) == {
        5: Trail(fix, "ctx.py", 3, []),
    }
    assert trace_lines(store, "HEAD", "ctx.py", {2: "    k=v,"}) == {
        2: Trail(black, "ctx.py", 2, []),
    }


@pytest.mark.parametrize(("before", "after", "line"), [
    pytest.param("result=value\n", "result=value,\n", 1, id="scalar-tuple"),
    pytest.param("result=(value)\n", "result=(value,)\n", 1, id="singleton-tuple"),
    pytest.param("result=obj[key]\n", "result=obj[key,]\n", 1, id="tuple-subscript"),
    pytest.param(call('label="a"'), call('label="a,"'), 2, id="inside-string"),
    pytest.param("make(\n    mode=one,\n    " + ARG + "\n)\n",
                 "make(\n    mode=two,\n    " + ARG + ",\n)\n", 3, id="changed-sibling-value"),
    pytest.param(call(ARG), call(ARG + ",", "other"), 2, id="changed-callee"),
    pytest.param("make(\n    mode=1,\n    size=2,\n    " + ARG + "\n)\n",
                 "make(\n    size=2,\n    mode=1,\n    " + ARG + ",\n)\n", 4, id="reordered"),
    pytest.param(call(ARG), "make(\n    " + ARG + ",\n    mode=1,\n)\n", 2, id="appended"),
    pytest.param(call(ARG).replace("\n)", "\n\n)"), call(ARG + ",").replace("\n)", "\n\n)"),
                 2, id="closing-parenthesis-on-a-later-line"),
    pytest.param("make(\n    session=(\n        first + second\n    )\n)\n",
                 "make(\n    session=(\n        first + second\n    ),\n)\n", 4, id="multiline-value"),
    pytest.param(call(ARG) + "def invalid(\n", call(ARG + ",") + "def invalid(\n", 2, id="invalid-context"),
    pytest.param("make(\n    session=cached,\n    " + ARG + "\n)\n",
                 "make(\n    session=cached,\n    " + ARG + ",\n)\n", 3, id="duplicate-keyword-syntax"),
    pytest.param(call(ARG) + call(ARG, "other"),
                 call(ARG + ",") + call("session=other.session", "other"), 2, id="duplicate-deletions"),
    pytest.param(call(ARG) + call("session=other.session", "other"),
                 call(ARG + ",") + call(ARG + ",", "other"), 2, id="duplicate-additions"),
    # The only comma-free candidate with the same spacing is from the other call.
    pytest.param(call("session = self.session") + call(ARG, "other"),
                 call(ARG + ",") + call("session = self.session,", "other"), 2, id="distinct-calls"),
    pytest.param("make(\n    choice=choose(a=1, *args),\n    " + ARG + "\n)\n",
                 "make(\n    choice=choose(*args, a=1),\n    " + ARG + ",\n)\n", 3,
                 id="argument-order-inside-value"),
    pytest.param("before = " + call(), "after = " + call(ARG + ","), 2, id="changed-statement"),
])
def test_rejects_unverified_comma_origins(make_repo, before, after, line):
    repo = make_repo([
        (FIX, {"ctx.py": before}),
        ("Reformat with black", {"ctx.py": after}),
    ])
    store = indexed(repo)
    black = run_git(str(repo), "rev-parse", "HEAD").strip()
    assert trace_lines(store, "HEAD", "ctx.py", {line: after.splitlines()[line - 1]}) == {
        line: Trail(black, "ctx.py", line),
    }


@pytest.mark.parametrize("path, subject, other", [
    ("ctx.py", "Adjust context call", False),
    ("ctx.txt", "Reformat with black", False),
    ("ctx.py", "Reformat with black", True),
])
def test_requires_a_python_refactor_and_same_file(make_repo, path, subject, other):
    base = {path: "make()\n", "other.py": call()} if other else {path: call()}
    change = {path: call(ARG + ","), "other.py": "make()\n"} if other else {path: call(ARG + ",")}
    repo = make_repo([(FIX, base), (subject, change)])
    store = indexed(repo)
    head = run_git(str(repo), "rev-parse", "HEAD").strip()
    assert trace_lines(store, "HEAD", path, {2: ARG + ","}) == {2: Trail(head, path, 2)}


def test_exact_origin_precedes_the_comma_fallback(make_repo, monkeypatch):
    repo = make_repo([
        (FIX, {"ctx.py": call(), "other.py": call(ARG + ",")}),
        ("Reformat with black", {"ctx.py": call(ARG + ","), "other.py": "make()\n"}),
    ])
    store = indexed(repo)
    fix, black = [run_git(str(repo), "rev-parse", rev).strip() for rev in ("HEAD^", "HEAD")]

    def fail_load(*args):
        pytest.fail("exact lookup requested comma context")

    monkeypatch.setattr(RefactorOrigins, "_load_comma_pairs", fail_load)
    assert trace_lines(store, "HEAD", "ctx.py", {2: ARG + ","}) == {
        2: Trail(fix, "other.py", 2, [black]),
    }


def test_comma_inside_comment_only_matches_through_existing_normalization(make_repo):
    before = ARG + " # retained"
    after = before + ","
    repo = make_repo([(FIX, {"ctx.py": call(before)}), ("Reformat with black", {"ctx.py": call(after)})])
    sha = run_git(str(repo), "rev-parse", "HEAD").strip()
    origins = RefactorOrigins(str(repo))
    assert origins.find(sha, "ctx.py", after) == ("ctx.py", 2)
    assert origins.find_comma(sha, "ctx.py", 2, after) is None


def test_duplicate_heavy_bucket_does_not_load_context(make_repo, monkeypatch):
    before = call() * 128
    after = call(ARG + ",") * 128
    repo = make_repo([(FIX, {"ctx.py": before}), ("Reformat with black", {"ctx.py": after})])
    sha = run_git(str(repo), "rev-parse", "HEAD").strip()
    origins = RefactorOrigins(str(repo))
    assert origins.find(sha, "ctx.py", ARG + ",") is None

    def fail_load(*args):
        pytest.fail("an ambiguous predecessor requested comma context")

    monkeypatch.setattr(RefactorOrigins, "_load_comma_pairs", fail_load)
    for line in range(2, 128 * 3, 3):
        assert origins.find_comma(sha, "ctx.py", line, ARG + ",") is None


def test_comma_cache_reuses_context_and_checks_the_actual_added_row(make_repo, monkeypatch):
    repo = make_repo([
        (FIX, {"ctx.py": call()}),
        ("Reformat with black", {"ctx.py": call(ARG + ",")}),
    ])
    sha = run_git(str(repo), "rev-parse", "HEAD").strip()
    origins = RefactorOrigins(str(repo))
    origins.prefetch([sha, sha])
    commands = []

    def record_git(repo, *args):
        commands.append(args)
        return run_git(repo, *args)

    monkeypatch.setattr(trace_module, "run_git", record_git)
    for _ in range(20):
        assert origins.find(sha, "ctx.py", ARG + ",") is None
        assert origins.find_comma(sha, "ctx.py", 2, ARG + ",") == (2, "    " + ARG)
        assert origins.find_comma(sha, "ctx.py", 3, ARG + ",") is None
        assert origins.find_comma(sha, "ctx.py", 2, "session = self.session,") is None
    assert [args[0] for args in commands] == ["diff", "show", "show"]


def test_comma_cache_evicts_the_least_recently_used_file_at_eight_entries(make_repo, monkeypatch):
    paths = [f"ctx_{i}.py" for i in range(9)]
    arguments = [f"session=value_{i}" for i in range(9)]
    repo = make_repo([
        (FIX, {path: call(arg) for path, arg in zip(paths, arguments)}),
        ("Reformat with black", {path: call(arg + ",") for path, arg in zip(paths, arguments)}),
    ])
    sha = run_git(str(repo), "rev-parse", "HEAD").strip()
    origins = RefactorOrigins(str(repo))
    origins.prefetch([sha])
    load = origins._load_comma_pairs
    loads = []

    def record_load(commit, path):
        loads.append(path)
        return load(commit, path)

    monkeypatch.setattr(origins, "_load_comma_pairs", record_load)
    for i in (*range(8), 0, 8, 0, 1):
        assert origins.find_comma(sha, paths[i], 2, arguments[i] + ",") == (2, "    " + arguments[i])
    assert loads == [*paths, paths[1]]
    assert len(origins._comma_cache) == 8


def test_missing_comma_context_keeps_attribution_and_is_cached(make_repo, monkeypatch):
    repo = make_repo([(FIX, {"ctx.py": call()}), ("Reformat with black", {"ctx.py": call(ARG + ",")})])
    store = indexed(repo)
    sha = run_git(str(repo), "rev-parse", "HEAD").strip()
    origins = RefactorOrigins(str(repo))
    missing = []

    def without_blob(repo, *args):
        if args == ("show", f"{sha}^:ctx.py"):
            missing.append(args)
            raise trace_module.subprocess.CalledProcessError(128, args)
        return run_git(repo, *args)

    monkeypatch.setattr(trace_module, "run_git", without_blob)
    for _ in range(3):
        assert trace_lines(store, "HEAD", "ctx.py", {2: ARG + ","}, origins=origins) == {
            2: Trail(sha, "ctx.py", 2),
        }
    assert len(missing) == 1
