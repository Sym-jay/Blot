from blot.diff import parse_unified_diff, python_docstring_lines, read_changes

SAMPLE = """\
diff --git a/app.py b/app.py
index 1111111..2222222 100644
--- a/app.py
+++ b/app.py
@@ -10,2 +10 @@ def setup():
-    self_ref = weakref.ref(self)
-    view = lambda: self_ref().send()
+    view = lambda: self.send()
diff --git a/new.py b/new.py
new file mode 100644
--- /dev/null
+++ b/new.py
@@ -0,0 +1,2 @@
+x = 1
+y = 2
"""


def test_parse_removed_and_added_lines():
    app, new = parse_unified_diff(SAMPLE)
    assert app.path == "app.py" and app.old_path == "app.py"
    assert app.removed == [(10, "    self_ref = weakref.ref(self)"), (11, "    view = lambda: self_ref().send()")]
    assert app.added == [(10, "    view = lambda: self.send()")]
    assert new.old_path is None
    assert new.added == [(1, "x = 1"), (2, "y = 2")]


def test_read_staged_changes(make_repo):
    repo = make_repo([("feat: add a", {"a.py": "one\ntwo\n"})])
    (repo / "a.py").write_text("one\nTWO\n", encoding="utf-8")
    import subprocess

    subprocess.run(["git", "-C", str(repo), "add", "a.py"], check=True)
    (change,) = read_changes(str(repo))
    assert change.removed == [(2, "two")]
    assert change.added == [(2, "TWO")]


def test_python_docstring_lines_only_returns_real_docstrings():
    source = '''\
"""module docs = enabled()"""

DATA = """assigned = payload()
still data"""
"""a later string expression, not a docstring = value()"""

class Example:
    """Class docs containing items[0].
    More class docs.
    """

    def method(self):
        """Method docs.
        @decorator(option=True)
        """
        template = """runtime = render()"""
        """a later function string expression = value()"""

async def async_method():
    """Async docs with values[0] = call()."""
'''
    lines = python_docstring_lines(source)
    assert lines is not None
    numbered = {text: i for i, text in enumerate(source.splitlines(), 1)}
    assert numbered['"""module docs = enabled()"""'] in lines
    assert numbered['    """Class docs containing items[0].'] in lines
    assert numbered["        @decorator(option=True)"] in lines
    assert numbered['    """Async docs with values[0] = call()."""'] in lines
    assert numbered['DATA = """assigned = payload()'] not in lines
    assert numbered['"""a later string expression, not a docstring = value()"""'] not in lines
    assert numbered['        template = """runtime = render()"""'] not in lines
    assert numbered['        """a later function string expression = value()"""'] not in lines


def test_python_docstring_lines_handles_empty_and_invalid_source():
    assert python_docstring_lines("value = 1\n") == set()
    assert python_docstring_lines("def broken(\n") is None


# --- issue #36: arguments appended to a fix's call or def ------------------------------------
# Contract (maintainer, issue #36): the fix's existing arguments stay identical, token for token
# and in order; new arguments only at the end of that same list, before its closing paren, at the
# same nesting level; appended arguments may contain anything (`=`, `?`); for a `def`, only
# appended parameters with a default; a reflow must stay inside that one call's parentheses.

from blot.diff import KeptLines  # noqa: E402


def test_kept_django_keyword_argument_appended_over_a_reflowed_call():
    # Django d2e59b77, django/contrib/gis/forms/fields.py (labelled noise, cited dd65607)
    kept = KeptLines([
        "                    value = GEOSGeometry(",
        "                        value, max_geom_collections=self.max_geom_collections",
        "                    )",
    ])
    assert kept.keeps("                    value = GEOSGeometry(value)")


def test_kept_django_keyword_argument_appended_on_one_line():
    # Django d2e59b77, django/contrib/gis/forms/widgets.py: same pattern, no reflow
    kept = KeptLines(["            return GEOSGeometry(value, max_geom_collections=self.max_geom_collections)"])
    assert kept.keeps("            return GEOSGeometry(value)")


def test_kept_django_def_gains_a_parameter_with_a_default():
    # Django d2e59b77, django/contrib/gis/geos/geometry.py (labelled noise, cited a413ef2)
    kept = KeptLines([
        "    def _from_wkb(cls, wkb, max_geom_collections=MAX_GEOM_COLLECTIONS):",
        "        return wkb_r().read(wkb, max_geom_collections)",
    ])
    assert kept.keeps("    def _from_wkb(cls, wkb):")


def test_kept_react_ternary_argument_appended_over_six_lines():
    # React cbb046ab, ReactFiberHooks.js (labelled noise, cited a8f971b): 6 added lines, so the
    # call spans more than KeptLines' default 5-line window, and the new argument holds a `?`
    added = [
        "  const result = trackUsedThenable(",
        "    thenableState,",
        "    thenable,",
        "    index,",
        "    __DEV__ ? currentlyRenderingFiber : null,",
        "  );",
    ]
    assert len(added) >= 6
    assert KeptLines(added).keeps("  const result = trackUsedThenable(thenableState, thenable, index);")


def test_kept_positional_argument_appended_at_the_end():
    # React cbb046ab, ReactChildFiber.js: a plain positional argument at the end
    kept = KeptLines(["  return trackUsedThenable(thenableState, thenable, index, null);"])
    assert kept.keeps("  return trackUsedThenable(thenableState, thenable, index);")


def test_kept_several_arguments_appended_at_the_end():
    kept = KeptLines(["    value = GEOSGeometry(value, srid=self.srid, max_geom_collections=limit or 1)"])
    assert kept.keeps("    value = GEOSGeometry(value)")


def test_edited_when_an_existing_argument_changes():
    removed = "    value = GEOSGeometry(value == expected)"
    assert not KeptLines(["    value = GEOSGeometry(value != expected, max_geom_collections=1)"]).keeps(removed)
    assert not KeptLines(["    value = GEOSGeometry(value is not None, max_geom_collections=1)"]).keeps(
        "    value = GEOSGeometry(value is None)")
    assert not KeptLines(["    value = GEOSGeometry(not value, max_geom_collections=1)"]).keeps(
        "    value = GEOSGeometry(value)")


def test_edited_when_an_original_argument_is_replaced_or_removed():
    removed = "    geom = GEOSGeometry(value, srid)"
    assert not KeptLines(["    geom = GEOSGeometry(value, other, max_geom_collections=1)"]).keeps(removed)
    assert not KeptLines(["    geom = GEOSGeometry(value, max_geom_collections=1)"]).keeps(removed)
    assert not KeptLines(["    geom = GEOSGeometry(srid, max_geom_collections=1)"]).keeps(removed)


def test_edited_when_an_argument_is_added_at_the_start_or_in_the_middle():
    removed = "  const result = trackUsedThenable(thenableState, thenable, index);"
    first = "  const result = trackUsedThenable(__DEV__ ? fiber : null, thenableState, thenable, index);"
    middle = "  const result = trackUsedThenable(thenableState, __DEV__ ? fiber : null, thenable, index);"
    assert not KeptLines([first]).keeps(removed)
    assert not KeptLines([middle]).keeps(removed)
    # a JS default parameter inserted before an existing one
    assert not KeptLines(["function read(wkb, limit = 1, srid) {"]).keeps("function read(wkb, srid) {")


def test_edited_when_a_def_gains_a_required_parameter():
    assert not KeptLines(["    def _from_wkb(cls, wkb, max_geom_collections):"]).keeps("    def _from_wkb(cls, wkb):")


def test_edited_when_the_callee_changes():
    removed = "                    value = GEOSGeometry(value)"
    assert not KeptLines(["                    value = OGRGeometry(value, max_geom_collections=1)"]).keeps(removed)


def test_edited_for_logic_operator_changes():
    # the guards #36 must keep: `is None` -> `is not None`, `==` -> `!=`, `if a` -> `if a = b`
    assert not KeptLines(["if state.subdomain is not None:"]).keeps("if state.subdomain is None:")
    assert not KeptLines(["if request.method != expected:"]).keeps("if request.method == expected:")
    assert not KeptLines(["if a = b:"]).keeps("if a:")


def test_edited_when_the_change_is_outside_the_argument_list():
    removed = "    value = GEOSGeometry(value)"
    assert not KeptLines(["    value = not GEOSGeometry(value, max_geom_collections=1)"]).keeps(removed)
    assert not KeptLines(["    if GEOSGeometry(value, max_geom_collections=1) and strict():"]).keeps(
        "    if GEOSGeometry(value):")


def test_edited_when_a_reflow_leaves_the_original_call():
    # the old arguments now sit in another call: matching may not span two calls' parentheses
    kept = KeptLines([
        "    value = GEOSGeometry(other)",
        "    wrap(value, max_geom_collections=1)",
    ])
    assert not kept.keeps("    value = GEOSGeometry(value)")


# baseline gaps the old matcher let through (it only refused inserted `_LOGIC` tokens)

def test_edited_when_a_plain_positional_argument_is_added_at_the_start():
    assert not KeptLines(["    geom = GEOSGeometry(extra, value, srid)"]).keeps("    geom = GEOSGeometry(value, srid)")


def test_edited_when_a_plain_positional_argument_is_added_in_the_middle():
    assert not KeptLines(["    geom = GEOSGeometry(value, extra, srid)"]).keeps("    geom = GEOSGeometry(value, srid)")


def test_edited_when_an_original_argument_is_wrapped_or_extended():
    removed = "    value = GEOSGeometry(value)"
    assert not KeptLines(["    value = GEOSGeometry(value.strip())"]).keeps(removed)
    assert not KeptLines(["    value = GEOSGeometry(value.strip(), max_geom_collections=1)"]).keeps(removed)
    assert not KeptLines(["    value = GEOSGeometry(clean(value))"]).keeps(removed)


def test_edited_when_code_is_added_after_the_call_on_the_same_line():
    assert not KeptLines(["    return parse(value) or fallback"]).keeps("    return parse(value)")
    assert not KeptLines(["    return parse(value, strict=True) or fallback"]).keeps("    return parse(value)")


def test_edited_when_a_def_gains_a_required_or_star_parameter():
    removed = "    def _from_wkb(cls, wkb):"
    assert not KeptLines(["    def _from_wkb(cls, wkb, max_geom_collections):"]).keeps(removed)
    assert not KeptLines(["    def _from_wkb(cls, wkb, *args):"]).keeps(removed)
    assert not KeptLines(["    def _from_wkb(cls, wkb, limit: int):"]).keeps(removed)


def test_edited_when_an_argument_is_appended_to_a_nested_call():
    # the new argument lands inside an existing argument of the outer call: that argument changed
    assert not KeptLines(["    value = GEOSGeometry(clean(value, strict=True))"]).keeps(
        "    value = GEOSGeometry(clean(value))")


def test_added_lines_from_separate_hunks_are_not_combined():
    lines = ["    value = GEOSGeometry(", "        value, max_geom_collections=1", "    )"]
    assert KeptLines(lines, numbers=[40, 41, 42]).keeps("    value = GEOSGeometry(value)")
    assert not KeptLines(lines, numbers=[40, 41, 90]).keeps("    value = GEOSGeometry(value)")


# reformatting the old matcher already accepted, which the stricter rule must keep accepting

def test_kept_when_black_adds_a_trailing_comma():
    kept = KeptLines(["    result = compute(", "        first,", "        second,", "    )"])
    assert kept.keeps("    result = compute(first, second)")


def test_kept_when_black_adds_trailing_commas_to_nested_calls():
    # 7 lines: past the old 5-line window, so this one is new behaviour, not compatibility
    removed = "    result = compute(first, second(third, fourth))"
    kept = KeptLines([
        "    result = compute(",
        "        first,",
        "        second(",
        "            third,",
        "            fourth,",
        "        ),",
        "    )",
    ])
    assert kept.keeps(removed)


def test_edited_when_wrapped_lines_are_joined():
    # Was a "kept" compatibility test until the audit. A join puts code from other removed lines
    # before or after this one on the same added line, which KeptLines can't tell apart from
    # code added around it (audit HIGH 1) or an argument inserted after a fragment (HIGH 2).
    assert not KeptLines(["    result = compute(first, second, third)"]).keeps("    result = compute(first, second,")
    assert not KeptLines(["    names = [first, second, third]"]).keeps("        first,")


def test_kept_when_an_expression_is_wrapped_in_parentheses():
    kept = KeptLines(["    return (", "        first_condition(value)", "        and second_condition(value)", "    )"])
    assert kept.keeps("    return first_condition(value) and second_condition(value)")


def test_kept_when_a_long_call_is_reflowed_one_argument_per_line():
    added = ["    value = build(", *[f"        arg{i}," for i in range(8)], "        extra=True,", "    )"]
    assert KeptLines(added).keeps("    value = build(" + ", ".join(f"arg{i}" for i in range(8)) + ")")


# found by comparing old and new verdicts over requests' history (561e4b68, 2a6f290b)

def test_kept_when_a_type_hint_holds_a_comma():
    # the parameter list's comma must not be matched to the one inside `dict[str, int]`
    kept = KeptLines(["    def set(self, name: str, value: dict[str, int] | None, **kwargs: Any) -> None:"])
    assert kept.keeps("    def set(self, name, value, **kwargs):")


def test_edited_when_a_fragment_closing_paren_moves_to_its_own_line():
    # Was "kept" (requests 2a6f290b, a Black reflow) until the audit: it has the same shape as
    # audit HIGH 2 (`value)` -> `extra,` / `value,` / `)`). Without the `(` the fragment closes,
    # a reflow and an inserted argument look alike, so the match fails closed.
    kept = KeptLines(['        "Old version of urllib3",', "        RequestsDependencyWarning,", "    )"])
    assert not kept.keeps("        RequestsDependencyWarning)")


def test_edited_when_a_balanced_fragment_is_joined_after_other_code():
    # Was "kept" (requests 2a6f290b, a Black join) until the audit: the added line has code before
    # and after the removed one, the same shape as `audit(); value = ...` (audit HIGH 1).
    kept = KeptLines(["        return ((lowerkey, keyval[1]) for (lowerkey, keyval) in self._store.items())"])
    assert not kept.keeps("            (lowerkey, keyval[1])")


# --- independent audit (HIGH 1-5): fail closed wherever the structure can't be verified -------

def test_audit_high1_code_before_the_line_is_an_edit():
    removed = "value = GEOSGeometry(value)"
    assert not KeptLines(["audit(); value = GEOSGeometry(value, limit=1)"]).keeps(removed)
    assert not KeptLines(["audit(); value = GEOSGeometry(value)"]).keeps(removed)


def test_audit_high1_code_after_the_line_is_an_edit():
    removed = "value = GEOSGeometry(value)"
    assert not KeptLines(["value = GEOSGeometry(value, limit=1); audit()"]).keeps(removed)
    assert not KeptLines(["value = GEOSGeometry(value); audit()"]).keeps(removed)


def test_audit_high2_fragment_without_its_opening_paren_fails_closed():
    # `git diff -U0` shows `value)` without the `(` it closes: an argument inserted before it,
    # or a parameter appended without a default, can't be told apart from a reflow
    assert not KeptLines(["extra,", "value,", ")"]).keeps("value)")
    assert not KeptLines(["wkb, max_geom_collections):"]).keeps("wkb):")


def test_audit_high2_fragment_without_brackets_fails_closed():
    assert not KeptLines(["    value, extra, srid"]).keeps("    value, srid")
    assert not KeptLines(["    value, srid, extra"]).keeps("    value, srid")


def test_audit_high3_optional_call_is_not_an_argument_list():
    removed = "result = fn?.(a, b)"
    assert not KeptLines(["result = fn?.(a, inserted, b)"]).keeps(removed)
    assert not KeptLines(["result = fn?.(inserted, a, b)"]).keeps(removed)


def test_audit_high3_generic_call_is_not_an_argument_list():
    removed = "result = fn<T>(a, b)"
    assert not KeptLines(["result = fn<T>(a, inserted, b)"]).keeps(removed)
    assert not KeptLines(["result = fn<T>(a.inner, b)"]).keeps(removed)
    assert not KeptLines(["result = fn<T, U>(a, b)"]).keeps(removed)


def test_audit_high4_class_bases_are_not_call_arguments():
    assert not KeptLines(["class Child(Base, Mixin):"]).keeps("class Child(Base):")
    assert not KeptLines(["class Child(Mixin, Base):"]).keeps("class Child(Base):")


def test_audit_high4_a_trailing_comma_can_turn_a_group_into_a_tuple():
    assert not KeptLines(["    return (value,)"]).keeps("    return (value)")


def test_audit_high4_nothing_is_inserted_into_a_group():
    assert not KeptLines(["    total = (first, extra, second)"]).keeps("    total = (first, second)")
    assert not KeptLines(["    total = (first, second, extra)"]).keeps("    total = (first, second)")


def test_audit_high5_equals_sign_inside_a_string_is_not_a_default():
    assert not KeptLines(['def f(a, b: "x=y"):']).keeps("def f(a):")
    assert not KeptLines(["def f(a, b: 'x=y'):"]).keeps("def f(a):")


# --- independent audit (MEDIUM) ---------------------------------------------------------------

def test_audit_medium_parentheses_inside_strings_are_not_structure():
    assert KeptLines(['    log("open (", level=1)']).keeps('    log("open (")')
    assert KeptLines(["    log(')', level=1)"]).keeps("    log(')')")


def test_audit_medium_keyword_only_parameters_with_defaults_are_appended_parameters():
    assert KeptLines(["    def read(self, wkb, *, limit=1):"]).keeps("    def read(self, wkb):")
    assert not KeptLines(["    def read(self, wkb, *, limit):"]).keeps("    def read(self, wkb):")
    assert not KeptLines(["    def read(self, wkb, *):"]).keeps("    def read(self, wkb):")


def test_audit_medium_a_javascript_method_is_a_definition_not_a_call():
    assert not KeptLines(["  read(wkb, limit) {"]).keeps("  read(wkb) {")
    assert KeptLines(["  read(wkb, limit = 1) {"]).keeps("  read(wkb) {")


def test_audit_medium_existing_type_hints_stay_cosmetic():
    assert KeptLines(["    count: int = compute(value)"]).keeps("    count = compute(value)")
    assert KeptLines(["    def read(self, wkb: bytes) -> Geometry:"]).keeps("    def read(self, wkb):")
    assert KeptLines(["function read(wkb: Buffer, srid: number) {"]).keeps("function read(wkb, srid) {")


def test_audit_medium_unrecognised_brackets_get_no_insertions():
    # the old rule (any token but `_LOGIC`) no longer applies anywhere
    assert not KeptLines(["    return await compute(value)"]).keeps("    return compute(value)")
    assert not KeptLines(["    names = [first, second, third]"]).keeps("    names = [first, second]")
    assert not KeptLines(["    options = {'a': 1, 'b': 2}"]).keeps("    options = {'a': 1}")
    assert not KeptLines(["    total = first * (second + third)"]).keeps("    total = first * second + third")


def test_audit_a_line_that_opens_a_bracket_at_its_end_is_still_checked():
    # requests 561e4b68: type hints on lines that open a multi-line literal or call
    assert KeptLines(["    __attrs__: list[str] = ["]).keeps("    __attrs__ = [")
    assert KeptLines(["function read(wkb): Geometry {"]).keeps("function read(wkb) {")
    # but nothing may be inserted after the bracket the line leaves open
    assert not KeptLines(["    __attrs__ = [extra,"]).keeps("    __attrs__ = [")
    assert not KeptLines(["    result = compute(extra"]).keeps("    result = compute(")
    assert not KeptLines(["    __attrs__ = sorted(["]).keeps("    __attrs__ = [")


def test_audit_a_comma_list_line_may_gain_a_trailing_comma():
    # requests 561e4b68: Black explodes a signature's parameter line and ends it with a comma
    kept = KeptLines(["        self,", "        chunk_size=ITER_CHUNK_SIZE,", "        decode_unicode=False,"])
    assert kept.keeps("        self, chunk_size=ITER_CHUNK_SIZE, decode_unicode=False")
    # a line without a top-level comma would become a tuple
    assert not KeptLines(["    timeout = DEFAULT_TIMEOUT,"]).keeps("    timeout = DEFAULT_TIMEOUT")
    assert not KeptLines(["    value = compute(a, b),"]).keeps("    value = compute(a, b)")


def test_kept_when_a_call_inside_a_list_or_dict_gains_an_argument():
    # Blot d27713d: the call is the list's element; its own argument list grows at the end
    assert KeptLines(["    evidence = [describe_commit(row, store)]"]).keeps("    evidence = [describe_commit(row)]")
    assert KeptLines(['    payload = {"geom": GEOSGeometry(value, limit=1)}']).keeps(
        '    payload = {"geom": GEOSGeometry(value)}')
    # inside another call's parentheses, or a group's, it is still that argument changing
    assert not KeptLines(["    total = (GEOSGeometry(value, limit=1))"]).keeps("    total = (GEOSGeometry(value))")


# --- second audit (HIGH): PEP 695 generic declarations are declarations, not calls ------------

def test_audit2_generic_def_gains_a_required_parameter():
    assert not KeptLines(["def f[T](a, b):"]).keeps("def f[T](a):")


def test_audit2_generic_def_gains_a_parameter_with_a_default():
    assert KeptLines(["def f[T](a, b=1):"]).keeps("def f[T](a):")


def test_audit2_generic_async_def_gains_a_required_parameter():
    assert not KeptLines(["async def f[T](a, b):"]).keeps("async def f[T](a):")


def test_audit2_generic_class_gains_a_base():
    assert not KeptLines(["class C[T](Base, Mixin):"]).keeps("class C[T](Base):")


def test_audit2_subscripted_callee_is_still_a_call():
    assert KeptLines(["factory[T](a, extra=True)"]).keeps("factory[T](a)")


# --- requests 2a6f290b (Black): parentheses around an `if` condition ------------------------

def test_kept_when_black_wraps_an_if_condition_in_parentheses():
    # requests 2a6f290b, requests/cookies.py (fix cfbbd15)
    kept = KeptLines([
        "        if (",
        '            hasattr(cookie.value, "startswith")',
        "            and cookie.value.startswith('\"')",
        "            and cookie.value.endswith('\"')",
        "        ):",
    ])
    assert kept.keeps(
        "        if hasattr(cookie.value, 'startswith') and cookie.value.startswith('\"')"
        " and cookie.value.endswith('\"'):"
    )
    while_ = KeptLines(["    while (", "        queue and not stopped", "    ):"])
    assert while_.keeps("    while queue and not stopped:")
    assert KeptLines(["    elif (", "        first or second", "    ):"]).keeps("    elif first or second:")


def test_edited_when_a_condition_wrap_is_partial_or_changes_the_condition():
    removed = "    if first and second:"
    for added in ("    if (first) and second:", "    if (first and second) or third:", "    if not (first and second):",
                  "    if (first and second,):", "    if (first and second)():", "    if (first and second) :  pass"):
        assert not KeptLines([added]).keeps(removed), added
    # only `if` / `elif` / `while` lines ending in `:` get the rule
    assert not KeptLines(["    if (first): return"]).keeps("    if first: return")
    assert not KeptLines(["    with (first, second):"]).keeps("    with first, second:")
    assert not KeptLines(["    for item in (items):"]).keeps("    for item in items:")
    assert not KeptLines(["    assert (first, 'message')"]).keeps("    assert first, 'message'")
    assert not KeptLines(["  if ((first)) {"]).keeps("  if (first) {")


def test_edited_when_a_join_moves_code_into_a_comment():
    # third audit: `#(` and `//(` open no bracket; what follows them on the line is a comment
    assert not KeptLines(["    value = 1  #( self.release_lock(value)"]).keeps("    self.release_lock(value)")
    assert not KeptLines(["  let value = 1; //( releaseLock(value);"]).keeps("  releaseLock(value);")
