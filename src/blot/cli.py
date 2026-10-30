"""blot: history-aware pre-commit checks.

Commands:
    index         build or refresh the local history index (.git/blot.db)
    check         run all checks against staged changes (pre-commit) or a commit range
    why           show the history behind a line: fix commits, PR, linked issues
    install-hook  add blot to .git/hooks/pre-commit
    bench         measure accuracy: bench cases, or replay recent commits (--replay N)
"""

from __future__ import annotations

import argparse
import os
import sys

from blot import __version__
from blot.checks import all_checks
from blot.config import load_config
from blot.report import render


def _cmd_index(args: argparse.Namespace) -> int:
    from blot.index import build_index

    result = build_index(args.repo, full=args.full)
    if not args.quiet:
        kind = "full" if result.full else "incremental"
        print(f"blot: indexed {result.commits} commits ({result.fixes} load-bearing fixes), "
              f"{kind}, in {result.seconds:.1f}s")
    if args.github:
        from blot.github import enrich
        from blot.store import Store

        store = Store.open(args.repo)
        got = enrich(args.repo, store)
        store.close()
        if got.status != "ok":
            print(f"blot: --github skipped: {got.status}", file=sys.stderr)
        elif not args.quiet:
            print(f"blot: GitHub: {got.fetched} issues/PRs fetched ({got.missing} not found); "
                  f"{got.upgraded} commits upgraded to bug fix, {got.demoted} demoted by their labels")
    return 0


def _open_index(repo: str):
    """Open the index, building or refreshing it first.

    The pre-commit framework installs Blot where users can't easily run `blot index`, so the hook
    must look after the index itself: one full build on first use, then cheap incremental refreshes.
    """
    from blot.index import build_index
    from blot.store import Store

    if not os.path.exists(Store.db_path(repo)):
        print("blot: first run, indexing this repo's history once (big repos can take a minute)...",
              file=sys.stderr)
    build_index(repo)
    return Store.open(repo)


def _cmd_check(args: argparse.Namespace) -> int:
    from blot.diff import range_base, read_changes

    try:
        config = load_config(args.repo)
        store = _open_index(args.repo)
        changes = read_changes(args.repo, rev_range=args.range)
        if args.range:  # removed lines are numbered as in the range's base, not as in HEAD
            store.rev = range_base(args.repo, args.range)
        findings = [f for check in all_checks(config) for f in check.run(changes, store, config)]
    except Exception as exc:  # Blot's own failures must never block a commit
        print(f"blot: internal error, skipping checks ({exc.__class__.__name__}: {exc})", file=sys.stderr)
        return 0
    print(render(findings, fmt=args.format), end="")
    return 1 if (args.strict and findings) else 0


def _cmd_why(args: argparse.Namespace) -> int:
    from blot.why import WhyError, explain, render

    store = _open_index(args.repo)
    try:
        path, explained = explain(store, args.location, args.rev)
    except WhyError as exc:
        print(f"blot: {exc}", file=sys.stderr)
        return 1
    text = render(store, path, explained)
    store.close()
    print(text, end="")
    return 0


def _cmd_install_hook(args: argparse.Namespace) -> int:
    from blot import hooks

    if args.uninstall:
        print(f"blot: pre-commit hook {hooks.uninstall(args.repo)}")
        return 0
    status = hooks.install(args.repo)
    print(f"blot: pre-commit hook {status} ({hooks.hook_path(args.repo)})")
    if status != "already installed":
        print("blot: the first commit indexes this repo's history (or run `blot index` now)")
    return 0


def _cmd_bench(args: argparse.Namespace) -> int:
    import dataclasses
    import json

    from blot import bench

    config = load_config(args.repo)
    if args.replay:
        def progress(i: int, total: int) -> None:
            if not args.quiet and (i % 50 == 0 or i == total):
                print(f"blot bench: replayed {i}/{total} commits", file=sys.stderr)

        result = bench.replay(args.repo, args.replay, config, max_files=args.max_files, progress=progress)
        if args.labels:
            labels = bench.load_labels(args.labels)
            if labels["head"] and not result.head.startswith(labels["head"]):
                print(f"blot bench: labels were made at {labels['head'][:9]}, this replay is at "
                      f"{result.head[:9]}; check out that commit to compare like with like", file=sys.stderr)
            new = bench.apply_labels(result, labels)
            if args.update_labels:
                bench.save_labels(args.labels, labels, result, new)
        print(bench.format_replay(result), end="")
        if args.json:
            with open(args.json, "w", encoding="utf-8") as f:
                json.dump(dataclasses.asdict(result), f, indent=2)
        return 0
    results = bench.run_cases(args.cases, args.workdir or bench.DEFAULT_WORKDIR, config)
    print(bench.format_cases(results), end="")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump([dataclasses.asdict(r) for r in results], f, indent=2)
    return 0 if all(r.correct for r in results) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="blot", description="History-aware pre-commit checks.")
    parser.add_argument("--version", action="version", version=f"blot {__version__}")
    parser.add_argument("--repo", default=".", help="path to the git repository (default: .)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("index", help="build or refresh the local history index")
    p.add_argument("--full", action="store_true", help="rebuild from scratch instead of incrementally")
    p.add_argument("--github", action="store_true", help="also fetch issue/PR titles and labels with the gh CLI")
    p.add_argument("--quiet", action="store_true", help="print nothing on success (for git hooks)")
    p.set_defaults(func=_cmd_index)

    p = sub.add_parser("check", help="run history checks on staged changes or a commit range")
    p.add_argument("--staged", action="store_true", help="check staged changes (the default)")
    p.add_argument("--range", metavar="A..B", help="check a commit range instead, e.g. origin/main..HEAD")
    p.add_argument("--strict", action="store_true", help="exit 1 when there are findings (blocks the commit)")
    p.add_argument("--format", choices=["text", "json"], default="text")
    p.set_defaults(func=_cmd_check)

    p = sub.add_parser("why", help="show the commit that explains a line, past refactors")
    p.add_argument("location", metavar="FILE:LINE", help="e.g. src/app.py:42 or src/app.py:40-48")
    p.add_argument("--rev", default="HEAD", help="explain the file as of this commit (default: HEAD)")
    p.set_defaults(func=_cmd_why)

    p = sub.add_parser("install-hook", help="add blot to .git/hooks/pre-commit")
    p.add_argument("--uninstall", action="store_true", help="remove blot's lines from the hook")
    p.set_defaults(func=_cmd_install_hook)

    p = sub.add_parser("bench", help="measure accuracy: bench cases, or replay recent commits")
    p.add_argument("--cases", default="bench/cases.json", help="cases file (default: bench/cases.json)")
    p.add_argument("--workdir", default=None, help="where case repos are cloned (default: ~/.cache/blot-bench)")
    p.add_argument("--replay", type=int, metavar="N", help="instead: re-check the last N commits of --repo")
    p.add_argument("--max-files", type=int, default=50, help="replay: skip commits touching more files")
    p.add_argument("--labels", metavar="FILE", help="replay: hand labels (useful/noise) to report precision")
    p.add_argument("--update-labels", action="store_true", help="replay: add unlabelled warnings to --labels FILE")
    p.add_argument("--json", metavar="FILE", help="also write full results as JSON")
    p.add_argument("--quiet", action="store_true", help="no progress output")
    p.set_defaults(func=_cmd_bench)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
