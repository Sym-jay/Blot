"""Recall on real regressions: of the commits that later turned out to cause a bug, how many did Blot
warn on at commit time?

    python bench/regressions.py collect --repo <clone> --out bench/regressions/<owner>-<repo>.json
    python bench/regressions.py run     --repo <clone> --file bench/regressions/<owner>-<repo>.json
    python bench/regressions.py report  bench/regressions/*.json

collect  reads the repo's whole history for fix commits that name the commit they fix by sha:
         `This reverts commit <sha>`, Django's `Regression in <sha>.`, "regression introduced by
         <sha>", `Fixes: <sha>`, and React's `Reverts facebook/react#N` (mapped to the one commit
         whose subject ends in `(#N)`). Each culprit is kept once (its earliest fix), then filtered:
         unresolvable, merge, root, revert of a revert (a re-land, not a bug), larger than
         --max-files, or touching no file Blot checks. The newest --cap pairs are kept. Judgements
         already in the file are kept.
run      replays each culprit like `blot bench --replay`: checked at its parent, seeing only its
         ancestors. Co-change counts come from the culprit's own ancestors (replay uses one early
         index for its whole window). It also records, for the lines the culprit removed, which
         commit `trace_lines` credits them to, so a miss can be judged: was a fix-added line there?
report   prints recall per repo from the hand judgements:
             verdict  hit-on-target | hit-elsewhere | miss-gap | miss-outside
             bug      true when the culprit really caused a bug (false: reverted for other reasons)

The clones must be at the `head` the file records, so the same commits are replayed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import asdict

from blot.bench import _ReplayStore
from blot.checks import all_checks
from blot.checks.base import is_skipped
from blot.config import Config, load_config
from blot.diff import KeptLines, is_meaningful_line, normalize_line, parse_unified_diff
from blot.git import run_git
from blot.index import _read_history, build_index
from blot.store import Store
from blot.trace import RefactorOrigins, trace_lines

SHA = r"([0-9a-f]{7,40})\b"
# (kind, pattern); the sha is the last group
PATTERNS = [
    ("revert", re.compile(r"This reverts commit " + SHA, re.I)),
    ("regression", re.compile(r"\bregress(?:ion|ed)\b(?: was)?(?: (?:in|from|introduced|caused))?"
                              r"(?: (?:in|by|from))?(?: commit)? " + SHA, re.I)),
    ("introduced", re.compile(r"\b(?:bug|failure|crash|breakage|problem|issue|error)s? (?:was )?"
                              r"(?:introduced|caused) (?:in|by)(?: commit)? " + SHA, re.I)),
    ("broken", re.compile(r"\b(?:broken|broke|caused) (?:in|by)(?: commit)? " + SHA, re.I)),
    ("fixes-trailer", re.compile(r"^Fixes: " + SHA, re.I | re.M)),
]
PR_PATTERNS = [
    ("revert-pr", re.compile(r"^Reverts (?:[\w.-]+/[\w.-]+)?#(\d+)\s*$", re.I | re.M)),
    ("regression-pr", re.compile(r"\bregression (?:from|in|introduced in|introduced by|caused by) #(\d+)", re.I)),
]
_PR_SUBJECT = re.compile(r"\(#(\d+)\)\s*$")
_REC, _FLD = "\x1e", "\x1f"


def find_references(message: str) -> list[tuple[str, str]]:
    """(kind, sha or "#N") for every culprit a commit message names."""
    found: list[tuple[str, str]] = []
    for kind, pattern in PATTERNS:
        found += [(kind, m.group(m.lastindex).lower()) for m in pattern.finditer(message)]
    for kind, pattern in PR_PATTERNS:
        found += [(kind, "#" + m.group(1)) for m in pattern.finditer(message)]
    seen, unique = set(), []
    for kind, ref in found:
        if ref not in seen:
            seen.add(ref)
            unique.append((kind, ref))
    return unique


def _log(repo: str) -> list[tuple[str, str, str, str]]:
    """(sha, date, subject, body) for every non-merge commit, newest first."""
    out = run_git(repo, "log", "--no-merges", f"--format={_REC}%H{_FLD}%aI{_FLD}%s{_FLD}%B", "HEAD")
    return [tuple(rec.split(_FLD, 3)) for rec in out.split(_REC)[1:]]


def _resolve(repo: str, ref: str) -> str | None:
    done = subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}"],
                          capture_output=True, text=True)
    return done.stdout.strip() or None if done.returncode == 0 else None


def _is_ancestor(repo: str, older: str, newer: str) -> bool:
    return subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor", older, newer],
                          capture_output=True).returncode == 0


def _parents(repo: str, sha: str) -> list[str]:
    return run_git(repo, "rev-list", "--parents", "-n", "1", sha).split()[1:]


def _changes(repo: str, parent: str, sha: str):
    return parse_unified_diff(run_git(repo, "diff", "-U0", "--no-color", "--no-ext-diff", "-M", parent, sha))


def collect(repo: str, config: Config, max_files: int = 50, cap: int = 40) -> dict:
    log = _log(repo)
    by_pr: dict[str, list[str]] = {}
    for sha, _, subject, _ in log:
        if m := _PR_SUBJECT.search(subject):
            by_pr.setdefault("#" + m.group(1), []).append(sha)
    subjects = {sha: subject for sha, _, subject, _ in log}
    dropped: Counter = Counter()
    pairs: dict[str, dict] = {}
    candidates = 0
    for sha, date, subject, body in reversed(log):  # oldest first: a culprit keeps its earliest fix
        for kind, ref in find_references(body):
            candidates += 1
            if ref.startswith("#"):
                shas = by_pr.get(ref, [])
                if len(shas) != 1:
                    dropped["pr not mapped to exactly one commit"] += 1
                    continue
                culprit = shas[0]
            else:
                culprit = _resolve(repo, ref)
            if culprit is None or culprit == sha or not _is_ancestor(repo, culprit, sha):
                dropped["sha not found or not an ancestor of the fix"] += 1
                continue
            if culprit in pairs:
                dropped["culprit already named by an earlier fix"] += 1
                continue
            parents = _parents(repo, culprit)
            if len(parents) != 1:
                dropped["merge" if parents else "root commit"] += 1
                continue
            culprit_subject = subjects.get(culprit) or run_git(repo, "log", "-1", "--format=%s", culprit).strip()
            if culprit_subject.lower().startswith(('revert "', "revert '")) or "This reverts commit" in \
                    run_git(repo, "log", "-1", "--format=%B", culprit):
                dropped["culprit is itself a revert (a re-land, not a bug)"] += 1
                continue
            changes = _changes(repo, parents[0], culprit)
            if len(changes) > max_files:
                dropped[f"more than {max_files} files"] += 1
                continue
            checked = [c.path for c in changes if not is_skipped(c.path, config)]
            if not checked:
                dropped["touches no checked file (tests, docs, generated)"] += 1
                continue
            fix_changes = _changes(repo, _parents(repo, sha)[0], sha) if _parents(repo, sha) else []
            pairs[culprit] = {
                "culprit": culprit[:10], "culprit_subject": culprit_subject,
                "fix": sha[:10], "fix_subject": subject, "fix_date": date[:10], "kind": kind, "ref": ref,
                "fix_files": sorted({c.old_path or c.path for c in fix_changes
                                     if not is_skipped(c.old_path or c.path, config)}),
            }
    kept = sorted(pairs.values(), key=lambda p: p["fix_date"], reverse=True)
    if len(kept) > cap:
        dropped[f"older than the newest {cap}"] += len(kept) - cap
        kept = kept[:cap]
    return {"repo": repo, "head": run_git(repo, "rev-parse", "HEAD").strip(), "max_files": max_files,
            "cap": cap, "candidates": candidates, "dropped": dict(dropped), "pairs": kept}


def merge_judgements(new: dict, old: dict) -> dict:
    """Keep hand judgements (and run results) from an earlier file for pairs that are still there."""
    previous = {p["culprit"]: p for p in old.get("pairs", [])}
    for pair in new["pairs"]:
        for key in ("bug", "verdict", "note", "findings", "removed_origins", "ms"):
            if key in previous.get(pair["culprit"], {}):
                pair[key] = previous[pair["culprit"]][key]
    return new


# --- run --------------------------------------------------------------------------------


class AncestorCochange:
    """Store.partners over only the ancestors of one commit, from one read of the whole history.

    Same counting as index.build_index: commits touching 1..max_files paths count once per path,
    and once per pair of distinct paths.
    """

    def __init__(self, history: list[tuple[str, list[str]]], ancestors: set[str], max_files: int):
        self.commits = [paths for sha, paths in history if sha in ancestors and 0 < len(paths) <= max_files]
        self.totals = Counter(p for paths in self.commits for p in paths)

    def partners(self, path: str, min_support: int, min_confidence: float) -> list[tuple[str, int, float]]:
        total = self.totals.get(path, 0)
        if not total:
            return []
        support = Counter(p for paths in self.commits if path in paths for p in set(paths) if p != path)
        result = [(p, s, s / total) for p, s in support.items()
                  if s >= min_support and s / total >= min_confidence and s / self.totals[p] >= min_confidence]
        return sorted(result, key=lambda r: (-r[2], -r[1], r[0]))


def removed_origins(store, repo: str, parent: str, changes, config: Config) -> list[dict]:
    """For the culprit's removed lines that removed-fix would look at, the commit each is traced to."""
    moved = {normalize_line(t) for c in changes for _, t in c.added}
    origins = RefactorOrigins(repo)
    rows: list[dict] = []
    for change in changes:
        if change.old_path is None or change.deleted or not change.removed or is_skipped(change.old_path, config):
            continue
        kept = KeptLines([t for _, t in change.added], numbers=[n for n, _ in change.added])
        texts = {n: t for n, t in change.removed
                 if is_meaningful_line(normalize_line(t)) and normalize_line(t) not in moved and not kept.keeps(t)}
        if not texts:
            continue
        by_origin: dict[str, list[int]] = {}
        for n, trail in trace_lines(store, parent, change.old_path, texts, origins).items():
            by_origin.setdefault(trail.sha, []).append(n)
        for sha, lines in by_origin.items():
            row = store.get_commit(sha)
            rows.append({"path": change.old_path, "lines": sorted(lines)[:5], "count": len(lines),
                         "origin": sha[:10], "subject": row.subject if row else "",
                         "severity": row.severity if row else None, "refactor": bool(row and row.is_refactor)})
    return rows


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def hunks(repo: str, parent: str, sha: str) -> dict[str, tuple[str, list[tuple[int, int, int, int]]]]:
    """{old path: (new path, [(old start, old count, new start, new count)])} from `git diff -U0`."""
    result: dict[str, tuple[str, list[tuple[int, int, int, int]]]] = {}
    old = None
    for line in run_git(repo, "diff", "-U0", "--no-color", "--no-ext-diff", "-M", parent, sha).splitlines():
        if line.startswith("--- "):
            old = None if line == "--- /dev/null" else line[6:]
        elif line.startswith("+++ ") and old:
            result[old] = (old if line == "+++ /dev/null" else line[6:], [])
        elif old and (m := _HUNK.match(line)):
            a, b, c, d = m.groups()
            result[old][1].append((int(a), 1 if b is None else int(b), int(c), 1 if d is None else int(d)))
    return result


def _back(n: int, hs: list[tuple[int, int, int, int]]) -> int | None:
    """Line n of a diff's new side, as a line number on its old side; None if the diff changed it."""
    offset = 0
    for old_start, old_count, new_start, new_count in hs:
        if new_count and new_start <= n < new_start + new_count:
            return None
        if (new_count and new_start + new_count <= n) or (not new_count and new_start < n):
            offset += old_count - new_count
    return n + offset


def culprit_lines_fix_touched(repo: str, culprit: str, fix: str) -> set[tuple[str, int]]:
    """(path, line number in the culprit's version) of the lines the fix changed or wrote between.

    Lines are mapped back from the fix's parent to the culprit through `git diff culprit fix^`; a line
    changed in between belongs to neither. A revert re-adding what the culprit deleted lands between
    the two lines around that deletion.
    """
    parents = _parents(repo, fix)
    if not parents:
        return set()
    since = {new: (old, hs) for old, (new, hs) in hunks(repo, culprit, parents[0]).items()}
    touched: set[tuple[str, int]] = set()
    for path, (_, hs) in hunks(repo, parents[0], fix).items():
        at, between = since.get(path, (path, []))
        for start, count, _, _ in hs:
            for n in (range(start, start + count) if count else (start, start + 1)):
                if n >= 1 and (line := _back(n, between)) is not None:
                    touched.add((at, line))
    return touched


def fix_proximity(finding: dict, culprit_hunks: dict, touched: set[tuple[str, int]]) -> str:
    """'fix-touched-hunk' when the later fix changed what the culprit wrote in the warned hunk,
    'fix-touched-file' when it changed culprit lines elsewhere in that file, else 'other'."""
    new_path, file_hunks = culprit_hunks.get(finding["path"], (finding["path"], []))
    in_file = {n for p, n in touched if p == new_path}
    if finding["line"] is not None:
        for old_start, old_count, new_start, new_count in file_hunks:
            if old_start <= finding["line"] < old_start + max(old_count, 1):
                # what the culprit wrote in place of the warned lines, or the two lines around a deletion
                span = range(new_start, new_start + new_count) if new_count else (new_start, new_start + 1)
                if in_file.intersection(span):
                    return "fix-touched-hunk"
    return "fix-touched-file" if in_file else "other"


def run(data: dict, config: Config, progress=None, github: bool = False) -> dict:
    """With `github`, commit facts also use issue/PR labels, as after `blot index --github`
    (labels are read as they are today, as in `blot bench --replay --github`)."""
    repo = data["repo"]
    head = run_git(repo, "rev-parse", "HEAD").strip()
    if head != data["head"]:
        raise SystemExit(f"{repo} is at {head[:9]}, the file was collected at {data['head'][:9]}")
    tmp = tempfile.mkdtemp(prefix="blot-regressions-")
    try:
        build_index(repo, full=True, config=config, db_path=os.path.join(tmp, "full.db"))
        full = Store.open(repo, os.path.join(tmp, "full.db"))
        if github:
            from blot.github import enrich

            got = enrich(repo, full)
            data["github"] = got.status if got.status != "ok" else (
                f"{got.fetched} issues/PRs fetched, {got.upgraded} commits upgraded, {got.demoted} demoted")
        history = [(info.sha, paths) for info, _, paths in _read_history(repo, head)]
        checks = all_checks(config)
        for i, pair in enumerate(data["pairs"], 1):
            culprit = run_git(repo, "rev-parse", pair["culprit"]).strip()
            parent = _parents(repo, culprit)[0]
            ancestors = set(run_git(repo, "rev-list", parent).split())
            store = _ReplayStore(repo, full, AncestorCochange(history, ancestors, config.cochange_max_files_per_commit))
            store.rev = parent
            changes = _changes(repo, parent, culprit)
            t = time.perf_counter()
            findings = [asdict(f) for check in checks for f in check.run(changes, store, config)]
            pair["ms"] = int((time.perf_counter() - t) * 1000)
            pair["findings"] = [{k: f[k] for k in ("check", "path", "line", "evidence")} for f in findings]
            if findings:
                fix = run_git(repo, "rev-parse", pair["fix"]).strip()
                touched = culprit_lines_fix_touched(repo, culprit, fix)
                culprit_hunks = hunks(repo, parent, culprit)
                for f in pair["findings"]:
                    f["near_fix"] = fix_proximity(f, culprit_hunks, touched)
            pair["removed_origins"] = removed_origins(store, repo, parent, changes, config)
            pair.setdefault("bug", None)
            pair.setdefault("verdict", None)
            pair.setdefault("note", "")
            if progress:
                progress(i, len(data["pairs"]))
        full.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return data


# --- report -----------------------------------------------------------------------------

HITS = ("hit-on-target", "hit-elsewhere")


def summarize(data: dict) -> dict:
    """Counts over the pairs whose culprit really caused a bug (bug is not false)."""
    pairs = [p for p in data["pairs"] if p.get("bug") is not False]
    verdicts = Counter(p.get("verdict") for p in pairs)
    within = verdicts["hit-on-target"] + verdicts["hit-elsewhere"] + verdicts["miss-gap"]
    return {
        "pairs": len(data["pairs"]), "not_a_bug": len(data["pairs"]) - len(pairs), "bugs": len(pairs),
        "warned": sum(bool(p.get("findings")) for p in pairs),
        "on_target": verdicts["hit-on-target"], "elsewhere": verdicts["hit-elsewhere"],
        "miss_gap": verdicts["miss-gap"], "miss_outside": verdicts["miss-outside"],
        "unjudged": verdicts[None], "within_model": within,
    }


def _pct(a: int, b: int) -> str:
    return f"{a}/{b} ({100 * a / b:.0f}%)" if b else f"{a}/0"


def format_report(named: list[tuple[str, dict]]) -> str:
    lines = ["| Repo | Pairs | Real bugs | Warned | On target | Recall raw (on target) | Within model | "
             "Recall within model (on target) |", "|---|---|---|---|---|---|---|---|"]
    total: Counter = Counter()
    for name, data in named:
        s = summarize(data)
        total.update(s)
        lines.append(f"| {name} | {s['pairs']} | {s['bugs']} | {s['warned']} | {s['on_target']} | "
                     f"{_pct(s['on_target'], s['bugs'])} | {s['within_model']} | {_pct(s['on_target'], s['within_model'])} |")
    t = total
    lines.append(f"| **All** | {t['pairs']} | {t['bugs']} | {t['warned']} | {t['on_target']} | "
                 f"**{_pct(t['on_target'], t['bugs'])}** | {t['within_model']} | **{_pct(t['on_target'], t['within_model'])}** |")
    if t["unjudged"]:
        lines.append(f"\n{t['unjudged']} pairs are not judged yet.")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("collect")
    p.add_argument("--repo", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--max-files", type=int, default=50)
    p.add_argument("--cap", type=int, default=40)
    p = sub.add_parser("run")
    p.add_argument("--repo", required=True)
    p.add_argument("--file", required=True)
    p.add_argument("--github", action="store_true", help="also use issue/PR labels (needs the gh CLI)")
    p = sub.add_parser("report")
    p.add_argument("files", nargs="+")
    args = parser.parse_args(argv)

    if args.cmd == "collect":
        data = collect(args.repo, load_config(args.repo), args.max_files, args.cap)
        if os.path.exists(args.out):
            with open(args.out, encoding="utf-8") as f:
                data = merge_judgements(data, json.load(f))
        _save(args.out, data)
        print(f"{len(data['pairs'])} pairs from {data['candidates']} references; dropped: {data['dropped']}")
    elif args.cmd == "run":
        with open(args.file, encoding="utf-8") as f:
            data = json.load(f)
        data["repo"] = args.repo
        data = run(data, load_config(args.repo),
                   progress=lambda i, n: print(f"replayed {i}/{n}", file=sys.stderr), github=args.github)
        if data.get("github"):
            print(f"github: {data['github']}")
        _save(args.file, data)
        print(f"{sum(bool(p['findings']) for p in data['pairs'])}/{len(data['pairs'])} culprits warned")
    else:
        named = []
        for path in args.files:
            with open(path, encoding="utf-8") as f:
                named.append((os.path.basename(path).removesuffix(".json"), json.load(f)))
        print(format_report(named), end="")
    return 0


def _save(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=1)
        f.write("\n")


if __name__ == "__main__":
    sys.exit(main())
