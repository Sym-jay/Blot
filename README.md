# Blot

[![CI](https://github.com/Sym-jay/Blot/actions/workflows/ci.yml/badge.svg)](https://github.com/Sym-jay/Blot/actions/workflows/ci.yml) [![PyPI](https://img.shields.io/pypi/v/blot-git)](https://pypi.org/project/blot-git/)

<img src="assets/sora/orange/walk.svg" width="480" alt="Sora, the Blot slime, sliding back and forth">

**A pre-commit check that has read your project's history.**

Meet **Sora**, the slime who remembers every fix.

> Don't take down a fence until you know why it was put up.

Blot warns you *before you commit* when your change:

- **removes a fix**: deletes or edits code that was added to fix a bug
- **re-adds a bug**: brings back a line that a bug fix deliberately removed
- **forgets a related file**: changes `A` but not `B`, when the two almost always change together

It runs locally, needs no account or AI, and stays quiet unless the history gives it a real reason to speak.

<img src="assets/demo/demo.gif" width="760" alt="Blot catching a real Flask regression: simplifying self_ref = weakref.ref(self) triggers a removed-fix warning that traces past a 2023 refactor to the 2020 reference-cycle fix">

*A real run on Flask. Plain `git blame` points to a 2023 commit that only moved this line; Blot traces past it to the 2020 fix that explains it. (Regenerate with `python assets/demo/make_demo.py`.)*

> **Status:** pre-alpha. Milestones M1 and M2 are done: `blot index` builds the history index (Flask's 3,800 commits in ~2 s), `blot install-hook` wires up git, and `blot check` runs the three checks. On Flask it traces an edit to `self_ref = weakref.ref(self)` past the 2023 file split to the 2020 fix, in under 500 ms. Replayed on the last 500 commits of Flask, requests, Express, Django and React, it warns on 0.6–9.5% of commits, and about half of its warnings (57 of 109, each labelled by hand) are worth a reviewer's time; see [bench/RESULTS.md](bench/RESULTS.md). Roadmap: [PRD](docs/PRD.md#12-milestones-4-weeks-part-time).

## Meet Sora

<img src="assets/sora/orange/faces.svg" width="168" alt="Sora cycling through expressions" align="right">

Sora is Blot's slime. You'll see a different face depending on what Blot finds:

| | Face | When |
|---|---|---|
| <img src="assets/sora/orange/idle.svg" width="84"> | Idle | Checking your commit |
| <img src="assets/sora/orange/happy.svg" width="84"> | Happy | Nothing to worry about |
| <img src="assets/sora/orange/alert.svg" width="84"> | Alert | Found a fence: this line fixed a bug |
| <img src="assets/sora/orange/worried.svg" width="84"> | Worried | Risky change, take a look |
| <img src="assets/sora/orange/sleepy.svg" width="84"> | Sleepy | Indexing history |
| <img src="assets/sora/orange/sad.svg" width="84"> | Sad | A fix is being removed |

Sora comes in four warm palettes:

<img src="assets/sora/orange/walk.svg" width="240" alt="Orange Sora"> <img src="assets/sora/sage/walk.svg" width="240" alt="Sage Sora">
<img src="assets/sora/blue/walk.svg" width="240" alt="Blue Sora"> <img src="assets/sora/mustard/walk.svg" width="240" alt="Mustard Sora">

## Quickstart

```bash
pip install blot-git  # the PyPI name is blot-git; the command is just `blot`
cd your-repo
blot index            # optional: the first check builds .git/blot.db itself and keeps it current
blot install-hook     # adds Blot to .git/hooks/pre-commit without touching existing hooks
```

For the latest unreleased code, use `pip install git+https://github.com/Sym-jay/Blot` instead.

Remove it any time with `blot install-hook --uninstall`. Settings go in `[tool.blot]` in `pyproject.toml` or a `.blot.toml` file (see `src/blot/config.py`).

Or with the [pre-commit](https://pre-commit.com) framework, see `.pre-commit-hooks.yaml`.

## Ask why a line exists

```
$ blot why src/flask/app.py:355
src/flask/app.py:355
  | self_ref = weakref.ref(self)
  explained by  8efea0cc  2020-10-03  Break reference cycle created by default in Flask instances.  [bug fix]
  traced past   0ec7f713  2023-06-11  Split the App and Blueprint into Sansio and IO parts  [refactor]
```

`blot why FILE:LINE` (or `FILE:START-END`) finds the commit that explains a line, skipping commits that only moved it. Plain `git blame` stops at the refactor.

## Map a file's load-bearing lines

```
$ blot map src/flask/app.py
src/flask/app.py: 3 load-bearing lines in 2 ranges, from 2 fixes

355        | self_ref = weakref.ref(self)
             8efea0c "Break reference cycle created by default in Flask instances." (2020-10-03)
             traced past refactor 0ec7f71 "Split the App and Blueprint into Sansio and IO parts"

1061-1062  | rv = self.response_class()
           | rv.allow.update(methods)
             dbf55de "Fixed an issue where the default `OPTIONS` response was not exposing all valid methods in the `Allow` header." (2010-07-28, fixes #97)
             traced past refactor 0ec7f71 "Split the App and Blueprint into Sansio and IO parts"
```

`blot map FILE` runs `why` over a whole file and keeps the lines whose explaining commit is a bug fix:
the lines `blot check` would warn about if you changed them. Consecutive lines from one fix are grouped.
`--full` prints the whole file with the fix commit in the gutter, `--format json` gives the ranges and
commits as JSON, `--rev` maps an older version, and `--min-severity 1` also counts routine fixes.
On Flask's 1,625-line `app.py` it takes about 1.5 s.

## Give agents the history before they edit (MCP)

`blot mcp` is a [Model Context Protocol](https://modelcontextprotocol.io) server on stdio, so a coding
agent can ask about a file *before* it plans a change, instead of hearing about it from `blot guard` at
the last moment. It has two read-only tools:

| Tool | Same as | Arguments |
|---|---|---|
| `load_bearing_lines` | `blot map FILE` | `path`, optional `rev`, `min_severity` |
| `explain_lines` | `blot why FILE:START-END` | `path`, `start`, optional `end`, `rev` |

Each returns one paragraph per commit: short sha, date, subject, linked issues, the lines it explains
and any refactor traced past. The same data comes back as JSON in `structuredContent`.

```bash
blot install-mcp           # adds `blot` to .mcp.json at the repo root and builds the index
blot install-mcp --local   # or just for you, via `claude mcp add --scope local` (needs the claude CLI)
```

Claude Code asks each person once before it starts a server from `.mcp.json`. The entry names this
machine's Python by absolute path, like `install-agent-hook` does, so prefer `--local` if your team
commits `.mcp.json`. Other servers in the file are left alone; remove Blot's entry with
`blot install-mcp --uninstall` (add `--local` if you installed it that way). To register it by hand:

```bash
claude mcp add --scope local --transport stdio blot -- python -m blot mcp
```

The server works on the repository it is started in (or `blot --repo PATH mcp`). It speaks MCP
2026-07-28 and the earlier `initialize`-handshake versions (2024-11-05 to 2025-11-25), uses only the
standard library, writes nothing but protocol messages to stdout, and answers a bad path or an
untracked file with a tool error rather than exiting.

## Guard coding agents

Coding agents are the most likely to "clean up" a line they don't understand. `blot guard` is a
[Claude Code](https://code.claude.com/docs/en/hooks) `PreToolUse` hook on `Edit`, `Write` and `MultiEdit`:
it runs the `removed-fix` and `readded-bug` checks on the edit the agent is about to make, and hands any
finding to the agent as context:

```
blot: 1 warning

  ! removed-fix  src/flask/app.py:355
    You're removing or changing a line that a bug fix added.
    Check that the bug it fixed can't come back.
    8efea0c "Break reference cycle created by default in Flask instances." (2020-10-03)
    traced past refactor 0ec7f71 "Split the App and Blueprint into Sansio and IO parts"
Run `blot why FILE:LINE` for the full history before you change it.
```

```bash
blot install-agent-hook           # adds the hook to .claude/settings.json and builds the index
blot install-agent-hook --local   # or .claude/settings.local.json, which isn't committed
blot install-agent-hook --ask     # also ask you to approve edits that touch a fix
```

The hook command names this machine's Python by absolute path (like `install-hook`), so prefer `--local`
if your team commits `.claude/settings.json`. Remove it with `blot install-agent-hook --uninstall`
(add `--local` if you installed it there); other hooks in the file are left alone.

By default the guard only warns: Claude Code adds the warning to the agent's context with the edit's
result, and the normal permission flow is unchanged. With `--ask`, Claude Code stops before the edit and
asks you, showing the warning. The guard prints nothing when it has nothing to say, stays quiet for
files outside a git repo or not tracked at HEAD, and never blocks an edit because of its own errors. It
doesn't build a first index (that can take a minute): `install-agent-hook` does, or run `blot index`.
After that it refreshes the index incrementally. On Flask a guarded edit takes about 450 ms.
`forgot-related-file` is left out: it had no useful warnings among the 45 labelled in replays.

## Issue and PR titles (optional)

```bash
blot index --github
```

With the [GitHub CLI](https://cli.github.com) logged in, Blot fetches the titles and labels of the issues and PRs your fix commits mention, so warnings read `fixes #2935 "copy_current_request_context copies old session object"` (a real Flask issue) instead of a bare number. A `bug` label makes a commit count as a load-bearing fix; a fix-looking commit whose issues are only labelled `enhancement` or `documentation` is demoted. Refreshes only fetch what's new. Repos with GitHub Issues turned off (they track bugs elsewhere) are skipped.

## Use in CI

Run Blot on every pull request and see its findings as warnings on the changed lines:

```yaml
# .github/workflows/blot.yml
name: Blot
on: pull_request
jobs:
  blot:
    runs-on: ubuntu-latest
    steps:
      - uses: Sym-jay/Blot@v0.7.0
```

The action checks out the pull request with its full history, installs Blot, runs `blot index` and
`blot check --range origin/<base>..HEAD --format json`, and turns each finding into a warning annotation.
It never fails the job because of findings. Inputs: `blot-package` (what to `pip install`; default
`blot-git`, the latest PyPI release), `python-version` (default `3.13`) and `base-ref`
(default: the pull request's base branch).

## Measure it on your repo

```bash
blot bench --replay 500
```

Re-checks your last 500 commits as if each were being committed, and lists every warning Blot would have shown. See [bench/README.md](bench/README.md).

## Docs

- [What is this? (simple explanation)](docs/EXPLAINER.md)
- [Product requirements](docs/PRD.md)
- [Competitor tests](docs/competitors.md)
- [Contributing](CONTRIBUTING.md)

## License

MIT

Sora's body shape is based on [Animated Pixel Slime](https://rvros.itch.io/pixel-art-animated-slime) by rvros (CC0), recolored and given new expressions. Regenerate the sprites with `python assets/sora/build_sprites.py`.
