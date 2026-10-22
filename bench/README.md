# Bench

How we keep Blot's warnings accurate and rare. Two tools, both in `blot bench`.

## 1. Cases: known right answers

`cases.json` lists real edits on public repos, each labeled with what Blot **should** say.

```bash
blot bench                      # clones case repos into ~/.cache/blot-bench, prints precision/recall
blot bench --workdir D:\bench   # put the clones somewhere else (short paths help on Windows)
```

Each case:

| Field | Meaning |
|---|---|
| `id` | short unique name |
| `repo`, `at` | public repo URL and the commit to check out |
| `file` | file to edit |
| `replace` | `["old text", "new text"]`; the old text must appear **exactly once** in the file |
| `expect.check` | which check is being tested: `removed-fix`, `readded-bug` or `forgot-related-file` |
| `expect.warn` | `true` if it should warn, `false` if it must stay quiet |
| `expect.commit` | (warn cases) the commit the warning must cite; a prefix is fine |
| `notes` | why: link the PR or issue that explains the code |

Add **quiet cases** (`"warn": false`) as often as warning cases. They protect against false alarms.

## 2. Replay: how noisy is Blot on real history?

```bash
cd some-big-repo
blot bench --replay 500 --json replay.json
```

Replay re-checks the last 500 non-merge commits as if each were being committed at that moment,
using only the history that existed before it. It prints how many commits would have shown a warning
and lists every warning, so you can label each one as useful or noise. Noise patterns become fixes in
`classify.py` or the checks, plus a quiet case here so they never come back.

Target: **at most 1 warning per 10 commits**, and most of those should be worth reading.
See [RESULTS.md](RESULTS.md) for the latest numbers.
