"""`blot index --github`: issue and PR titles and labels, fetched with the user's `gh` login.

Only commits that look like fixes are looked up, in batches of 50 per GraphQL call, and refs that are
already stored are skipped, so a refresh costs nothing until new fixes land. Labels sharpen fix
detection: a `bug`/`regression`/`security` label makes a commit load-bearing, while a fix-looking
commit whose issues are only labelled `enhancement`/`documentation` is demoted.

Nothing is sent anywhere except GitHub's API, through `gh`. Repos with GitHub Issues disabled are
skipped: their `#N` refs usually point at another tracker (Django uses Trac), and looking them up on
GitHub would attach the wrong titles.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from blot.classify import adjust_for_labels
from blot.git import run_git
from blot.store import Store

BATCH = 50
_REMOTE = re.compile(r"github\.com[:/](?P<owner>[\w.-]+)/(?P<name>[\w.-]+?)(?:\.git)?/?$")
_NUMBER = re.compile(r"^(?:#|gh-)(\d+)$", re.IGNORECASE)

Runner = Callable[[str], dict]  # GraphQL query -> parsed JSON response


@dataclass
class EnrichResult:
    status: str  # "ok", or why nothing was fetched
    fetched: int = 0
    missing: int = 0
    upgraded: int = 0
    demoted: int = 0


def github_repo(repo: str) -> tuple[str, str] | None:
    for remote in ("origin", "upstream"):
        try:
            url = run_git(repo, "remote", "get-url", remote).strip()
        except subprocess.CalledProcessError:
            continue
        m = _REMOTE.search(url)
        if m:
            return m.group("owner"), m.group("name")
    return None


def gh_runner(query: str) -> dict:
    """Run a GraphQL query through `gh`. Partial results (some refs not found) still parse."""
    done = subprocess.run(["gh", "api", "graphql", "-f", f"query={query}"], capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    if not done.stdout.strip():
        raise RuntimeError(done.stderr.strip() or "gh api graphql failed")
    return json.loads(done.stdout)


def _label_names(node: dict) -> list[str]:
    return [n["name"] for n in (node.get("labels") or {}).get("nodes", [])]


_FIELDS = "title labels(first: 10) { nodes { name } }"


def _query(owner: str, name: str, numbers: list[int]) -> str:
    parts = [
        f"n{n}: issueOrPullRequest(number: {n}) {{ __typename "
        f"... on Issue {{ {_FIELDS} }} "
        f"... on PullRequest {{ {_FIELDS} closingIssuesReferences(first: 5) {{ nodes {{ {_FIELDS} }} }} }} }}"
        for n in numbers
    ]
    return f'{{ repository(owner: "{owner}", name: "{name}") {{ hasIssuesEnabled {" ".join(parts)} }} }}'


def enrich(repo: str, store: Store, runner: Runner | None = None) -> EnrichResult:
    found = github_repo(repo)
    if found is None:
        return EnrichResult("no GitHub remote (origin or upstream)")
    if runner is None:
        if shutil.which("gh") is None:
            return EnrichResult("the GitHub CLI `gh` isn't installed (https://cli.github.com)")
        runner = gh_runner
    owner, name = found

    fixes = store.commits_with_issues(min_severity=1)
    known = store.known_issue_refs()
    wanted = sorted({ref for row in fixes for ref in row.issues if _NUMBER.match(ref) and ref not in known})

    result = EnrichResult("ok")
    for i in range(0, len(wanted), BATCH):
        batch = wanted[i:i + BATCH]
        numbers = [int(_NUMBER.match(ref).group(1)) for ref in batch]
        try:
            data = (runner(_query(owner, name, numbers)).get("data") or {}).get("repository") or {}
        except (RuntimeError, json.JSONDecodeError) as exc:
            return EnrichResult(f"GitHub lookup failed: {exc}", result.fetched, result.missing)
        if i == 0 and data.get("hasIssuesEnabled") is False:
            return EnrichResult(f"GitHub Issues are disabled for {owner}/{name}; its #N refs probably point to "
                                "another tracker, so they were not looked up")
        rows = []
        for ref, n in zip(batch, numbers):
            node = data.get(f"n{n}")
            if not node:
                rows.append((ref, "", [], "missing"))
                result.missing += 1
                continue
            labels = _label_names(node)
            for linked in (node.get("closingIssuesReferences") or {}).get("nodes", []):
                labels += _label_names(linked)
            kind = "pr" if node["__typename"] == "PullRequest" else "issue"
            rows.append((ref, node.get("title", ""), sorted(set(labels)), kind))
            result.fetched += 1
        store.add_issues(rows)

    # re-judge every fix-looking commit with what its issues' labels say
    for row in fixes:
        labels = [label for ref in row.issues if (got := store.issue(ref)) for label in got[1]]
        new = adjust_for_labels(row.subject, row.severity, labels)
        if new != row.severity:
            store.set_severity(row.sha, new)
            if new > row.severity:
                result.upgraded += 1
            else:
                result.demoted += 1
    store.commit()
    return result
