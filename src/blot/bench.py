"""`blot bench`: measure how accurate Blot is.

Two modes:
    cases   Run bench/cases.json: scripted edits on public repos with a known right answer
            ("this edit must warn and cite commit X" / "this edit must stay quiet"), and report
            precision and recall per check.
    replay  Re-check the last N commits of a repo as if each one were being committed right then,
            to count how often Blot would have spoken up and to collect warnings to label by hand.

Replay never lets a commit see its own future. Commit facts (is it a fix? a refactor?) come from a
full index, which is safe because checks only ever ask about the commit's ancestors. Lines removed
by fixes only count if that fix is an ancestor of the commit being replayed, and co-change counts
come from a second index built just before the replay window.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field

from blot.checks import REGISTRY, all_checks
from blot.checks.base import Finding
from blot.config import Config
from blot.diff import parse_unified_diff
from blot.git import run_git
from blot.index import build_index
from blot.store import CommitRow, Store

DEFAULT_WORKDIR = os.path.join(os.path.expanduser("~"), ".cache", "blot-bench")


# --- replay -----------------------------------------------------------------------------


class _ReplayStore:
    """Looks like a Store to the checks, but hides anything newer than the commit being replayed."""

    def __init__(self, repo: str, full: Store, early: Store):
        self.repo = repo
        self.rev = "HEAD"
        self._full, self._early = full, early
        self._ancestor: dict[tuple[str, str], bool] = {}

    def get_commit(self, sha: str) -> CommitRow | None:
        return self._full.get_commit(sha)

    def describe_issue(self, ref: str) -> str:
        return self._full.describe_issue(ref)

    def fixes_that_removed(self, line_hash: str) -> list[tuple[str, str]]:
        return [(p, s) for p, s in self._full.fixes_that_removed(line_hash) if self._is_ancestor(s)]

    def partners(self, path: str, min_support: int, min_confidence: float) -> list[tuple[str, int, float]]:
        return self._early.partners(path, min_support, min_confidence)

    def _is_ancestor(self, sha: str) -> bool:
        key = (sha, self.rev)
        if key not in self._ancestor:
            done = subprocess.run(["git", "-C", self.repo, "merge-base", "--is-ancestor", sha, self.rev],
                                  capture_output=True)
            self._ancestor[key] = done.returncode == 0
        return self._ancestor[key]


@dataclass
class ReplayCommit:
    sha: str
    subject: str
    findings: list[dict]
    ms: int


@dataclass
class ReplayResult:
    repo: str
    head: str
    checked: int
    skipped_large: int
    seconds: float
    commits: list[ReplayCommit] = field(default_factory=list)

    @property
    def warned(self) -> list[ReplayCommit]:
        return [c for c in self.commits if c.findings]

    def by_check(self) -> dict[str, int]:
        counts = {cls.id: 0 for cls in REGISTRY}
        for c in self.commits:
            for f in c.findings:
                counts[f["check"]] = counts.get(f["check"], 0) + 1
        return counts


def replay(repo: str, count: int, config: Config, max_files: int = 50, progress=None) -> ReplayResult:
    start = time.perf_counter()
    head = run_git(repo, "rev-parse", "HEAD").strip()
    commits = run_git(repo, "rev-list", "--no-merges", f"--max-count={count}", "HEAD").split()[::-1]
    tmp = tempfile.mkdtemp(prefix="blot-replay-")
    try:
        build_index(repo, full=True, config=config, db_path=os.path.join(tmp, "full.db"))
        oldest_parent = _parent(repo, commits[0]) if commits else None
        early_db = os.path.join(tmp, "early.db")
        build_index(repo, full=True, config=config, upto=oldest_parent or commits[0], db_path=early_db)
        full, early = Store.open(repo, os.path.join(tmp, "full.db")), Store.open(repo, early_db)
        store = _ReplayStore(repo, full, early)
        checks = all_checks(config)
        result = ReplayResult(repo, head, 0, 0, 0.0)
        for i, sha in enumerate(commits, 1):
            parent = _parent(repo, sha)
            if parent is None:
                continue
            diff = run_git(repo, "diff", "-U0", "--no-color", "--no-ext-diff", "-M", parent, sha)
            changes = parse_unified_diff(diff)
            if len(changes) > max_files:
                result.skipped_large += 1
                continue
            store.rev = parent
            t = time.perf_counter()
            findings: list[Finding] = [f for check in checks for f in check.run(changes, store, config)]
            ms = int((time.perf_counter() - t) * 1000)
            subject = run_git(repo, "log", "-1", "--format=%s", sha).strip()
            result.commits.append(ReplayCommit(sha, subject, [asdict(f) for f in findings], ms))
            result.checked += 1
            if progress:
                progress(i, len(commits))
        full.close()
        early.close()
        result.seconds = time.perf_counter() - start
        return result
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _parent(repo: str, sha: str) -> str | None:
    parts = run_git(repo, "rev-list", "--parents", "-n", "1", sha).split()
    return parts[1] if len(parts) > 1 else None


def format_replay(result: ReplayResult) -> str:
    per10 = 10 * sum(len(c.findings) for c in result.commits) / max(result.checked, 1)
    lines = [
        f"blot bench --replay: {result.checked} commits checked in {result.seconds:.0f}s "
        f"({result.skipped_large} skipped as too large)",
        f"  commits with a warning: {len(result.warned)} ({100 * len(result.warned) / max(result.checked, 1):.1f}%)",
        f"  warnings per 10 commits: {per10:.2f}",
        "  by check: " + ", ".join(f"{k} {v}" for k, v in result.by_check().items()),
    ]
    findings = [f for c in result.commits for f in c.findings]
    if any("verdict" in f for f in findings):
        useful = sum(f["verdict"] == "useful" for f in findings)
        noise = sum(f["verdict"] == "noise" for f in findings)
        lines.append(f"  precision: {useful} useful / {useful + noise} labelled "
                     f"({100 * useful / max(useful + noise, 1):.0f}%), unlabelled: {len(findings) - useful - noise}")
    for c in result.warned:
        lines.append(f"\n  {c.sha[:9]} {c.subject}")
        for f in c.findings:
            where = f"{f['path']}:{f['line']}" if f["line"] else f["path"]
            mark = f"  [{f['verdict'] or '?'}]" if "verdict" in f else ""
            lines.append(f"    ! {f['check']}  {where}{mark}")
            lines.extend(f"      {e}" for e in f["evidence"])
    return "\n".join(lines) + "\n"


# --- replay labels ------------------------------------------------------------------------
# bench/labels/<owner>-<repo>.json stores a hand verdict ("useful" / "noise") per replay warning,
# so precision can be re-measured after every rule change. A label matches on (commit, check,
# path, what the warning cites), not the line number, so it survives small line drift.


def _cited(finding: dict) -> str:
    """The commit a warning cites, or for co-change the partner file it names."""
    first = finding["evidence"][0] if finding["evidence"] else finding["message"]
    return first.split()[0]


def _key(sha: str, check: str, path: str, cited: str) -> tuple[str, str, str, str]:
    return sha[:9], check, path, cited[:7]


def load_labels(path: str) -> dict:
    if not os.path.exists(path):
        return {"repo": "", "head": "", "labels": []}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def apply_labels(result: ReplayResult, labels: dict) -> list[dict]:
    """Set each finding's "verdict" (None when unlabelled); return entries for the unlabelled ones."""
    known = {_key(lb["sha"], lb["check"], lb["path"], lb["cited"]): lb["verdict"] for lb in labels["labels"]}
    new = []
    for c in result.commits:
        for f in c.findings:
            key = _key(c.sha, f["check"], f["path"], _cited(f))
            f["verdict"] = known.get(key)
            if key not in known:
                new.append({"sha": c.sha[:9], "subject": c.subject, "check": f["check"], "path": f["path"],
                            "line": f["line"], "cited": _cited(f), "verdict": None, "reason": ""})
    return new


def save_labels(path: str, labels: dict, result: ReplayResult, new: list[dict]) -> None:
    labels = {**labels, "repo": labels["repo"] or result.repo, "head": labels["head"] or result.head,
              "labels": labels["labels"] + new}
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(labels, f, indent=1)
        f.write("\n")


# --- cases ------------------------------------------------------------------------------


@dataclass
class CaseResult:
    id: str
    check: str
    expected_warn: bool
    warned: bool
    cited_right_commit: bool
    detail: str = ""
    applied: bool = True  # the scripted edit could be made; a quiet case that never ran proves nothing

    @property
    def correct(self) -> bool:
        if not self.applied:
            return False
        return self.cited_right_commit if self.expected_warn else not self.warned


def _repo_dir(url: str, workdir: str) -> str:
    # always a fresh clone under workdir: the bench checks out old commits and edits files,
    # so it must never run inside a repo the user is working in (even if `repo` is a local path)
    name = re.split(r"[\\/]", url.rstrip("/\\"))[-1].removesuffix(".git") or "repo"
    path = os.path.join(workdir, name)
    os.makedirs(workdir, exist_ok=True)
    if not os.path.isdir(os.path.join(path, ".git")):
        subprocess.run(["git", "clone", "-q", url, path], check=True)
    return path


def run_case(case: dict, workdir: str, config: Config) -> CaseResult:
    expect = case["expect"]
    repo = _repo_dir(case["repo"], workdir)
    at = case["at"]
    if subprocess.run(["git", "-C", repo, "cat-file", "-e", f"{at}^{{commit}}"], capture_output=True).returncode:
        subprocess.run(["git", "-C", repo, "fetch", "-q", "origin"], check=True)
    run_git(repo, "checkout", "-q", "--detach", "--force", at)
    db = os.path.join(workdir, f"{os.path.basename(repo)}-{at[:12]}.db")
    build_index(repo, config=config, upto=at, db_path=db)

    path, (old, new) = case["file"], case["replace"]
    file = os.path.join(repo, path)
    with open(file, encoding="utf-8", newline="") as f:
        text = f.read()
    if "\r\n" in text:  # checked out with CRLF (git on Windows); cases are written with LF
        old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
    if text.count(old) != 1:
        return CaseResult(case["id"], expect["check"], expect["warn"], False, False,
                          f"edit text found {text.count(old)} times in {path} (needs exactly 1)", applied=False)
    try:
        with open(file, "w", encoding="utf-8", newline="") as f:
            f.write(text.replace(old, new))
        changes = parse_unified_diff(run_git(repo, "diff", "-U0", "--no-color", "--no-ext-diff", "--", path))
        store = Store.open(repo, db)
        store.rev = at
        check = next(cls() for cls in REGISTRY if cls.id == expect["check"])
        findings = check.run(changes, store, config)
        store.close()
    finally:
        run_git(repo, "checkout", "-q", "--", path)

    want = expect.get("commit", "")
    cited = bool(findings) and any(e.startswith(want[:7]) for f in findings for e in f.evidence) if want else bool(findings)
    # co-change findings carry no evidence, so fall back to the message's first line
    detail = "; ".join((f.evidence or [f.message])[0].split("\n")[0] for f in findings) or "no warning"
    return CaseResult(case["id"], expect["check"], expect["warn"], bool(findings), cited, detail)


def run_cases(cases_path: str, workdir: str, config: Config) -> list[CaseResult]:
    with open(cases_path, encoding="utf-8") as f:
        cases = json.load(f)
    return [run_case(case, workdir, config) for case in cases]


def format_cases(results: list[CaseResult]) -> str:
    lines = []
    for r in results:
        mark = "ok  " if r.correct else "FAIL"
        want = "warn" if r.expected_warn else "quiet"
        lines.append(f"  {mark} {r.id}  [{r.check}, expect {want}]  {r.detail}")
    lines.append("")
    for cls in REGISTRY:
        rs = [r for r in results if r.check == cls.id]
        if not rs:
            continue
        tp = sum(r.expected_warn and r.cited_right_commit for r in rs)
        fn = sum(r.expected_warn and not r.cited_right_commit for r in rs)
        fp = sum(not r.expected_warn and r.warned for r in rs)
        precision = tp / (tp + fp) if tp + fp else None
        recall = tp / (tp + fn) if tp + fn else None
        fmt = lambda v: "n/a" if v is None else f"{v:.0%}"  # noqa: E731
        lines.append(f"  {cls.id}: {len(rs)} cases, precision {fmt(precision)}, recall {fmt(recall)} "
                     f"(tp {tp}, fp {fp}, fn {fn})")
    correct = sum(r.correct for r in results)
    lines.append(f"blot bench: {correct}/{len(results)} cases correct")
    return "\n".join(lines) + "\n"
