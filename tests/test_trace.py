"""Where tracing must stop. See bench/RESULTS.md, "Tried and dropped: tracing past unchanged lines"."""

from blot.checks.removed_fix import RemovedFix
from blot.index import build_index
from blot.store import Store
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
