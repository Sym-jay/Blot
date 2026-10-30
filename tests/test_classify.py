import pytest

from blot.classify import CommitInfo, fix_severity, is_refactor, linked_issues


def commit(subject, body="", files=1, added=5, removed=5, renames=0):
    return CommitInfo("abc", subject, body, files, added, removed, renames)


@pytest.mark.parametrize(
    "subject, body, expected",
    [
        ("Break reference cycle created by default in Flask instances.", "", 2),  # the Flask case
        ("Fix crash when session is empty", "", 2),
        ("Handle empty session", "Fixes #3761", 2),
        ("Fix typo in docs", "Fixes #12", 1),  # issue-linked but routine
        ("fix lint", "", 1),
        ("Fix stuff", "", 1),
        ("Add dark mode", "Closes #44", 0),  # "closes" is housekeeping, not a fix
        ("Bump version to 2.1", "", 0),
        ("Prevent SQL injection in search", "", 2),
        ("Fix wrong status code on redirect", "", 2),  # weak defect word + fix verb
        ("Wrong status code on redirect", "", 0),  # weak defect word alone
        # real Flask subjects that must NOT be load-bearing
        ("Documented redirect behaviour. Fixes #42", "", 1),
        ("Fix incorrect references to query in testing doc", "", 1),
        ("Added a new example for checksums on input data.  This fixes #601", "", 1),
        ("Fix minor typo in security doc", "", 1),
        ("Added note on send_file security.", "", 0),
        ("fixed possible security problem in module branch", "", 2),
        ("Fix broken link to Flask-WTF", "", 1),
        # Django's "Fixed #N -- <description>": the description decides
        ("Fixed #12090 -- Added admin actions to the admin change form.", "", 1),
        ("Fixed #37114 -- Dropped support for PostgreSQL 15 and PostGIS 3.2.", "", 1),
        ("Fixed #36560 -- Prevented UpdateCacheMiddleware from caching private responses.", "", 2),
        ("Fixed #30000 -- Fixed crash when saving a model with a NULL primary key.", "", 2),
        ("Fixed CVE-2026-15830 -- Mitigated potential DoS via nested geometry collections.", "", 2),
        ("Fix #2935: Copy current session object in copy_current_request_context", "", 2),
        # older ticket separators, judged by the description too
        ("Fixed #21189: Cleaned up usage of bare except clauses.", "", 1),
        ("Fixed #12882 - Moved the admin's jQuery into our own namespace to lower the risk of a clash", "", 1),
        ("Refs #18823 -- Corrected field name in an m2m manager error message.", "", 1),
        # tagged and "change" subjects that add behaviour
        ("[DevTools] upgrade to Manifest V3 (#25145)", "fixes #24522", 1),
        ("Change Flask.__init__ to accept two new keyword arguments (#1560)", "fixes #1559", 1),
        ("[Fizz] Fix crash when capturing the callsite of a stalled use()", "", 2),
    ],
)
def test_fix_severity(subject, body, expected):
    assert fix_severity(commit(subject, body)) == expected


def test_bug_label_is_always_load_bearing():
    assert fix_severity(commit("Tweak parser"), issue_labels=["Bug"]) == 2


@pytest.mark.parametrize(
    "info, expected",
    [
        (commit("Split the App and Blueprint into Sansio and IO parts"), True),  # the Flask trap
        (commit("Reformat with black", files=40, added=900, removed=900), True),
        (commit("Update paths", files=4, renames=3), True),
        (commit("Fix crash after moving config loader"), False),  # fixes are never refactors
        (commit("Add retry logic", added=80, removed=2), False),
    ],
)
def test_is_refactor(info, expected):
    assert is_refactor(info) is expected


def test_linked_issues_keeps_full_cve_ids():
    assert linked_issues(commit("sec: security patch for CVE-2024-51999")) == ["CVE-2024-51999"]


def test_linked_issues_dedupes_in_order():
    info = commit("Fix race in pool (#12)", "Fixes #12, see also gh-7 and JIRA-42. Not a URL: http://x/#3")
    assert linked_issues(info) == ["#12", "gh-7", "JIRA-42"]
