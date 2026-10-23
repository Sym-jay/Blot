# Blot

**A pre-commit check that has read your project's history.**

Blot warns you *before you commit* when your change:

- **removes a fix**: deletes or edits code that was added to fix a bug
- **re-adds a bug**: brings back a line that a bug fix deliberately removed
- **forgets a related file**: changes `A` but not `B`, when the two almost always change together

It runs locally, needs no account or AI, and stays quiet unless the history gives it a real reason to speak.

![Blot catching a real Flask regression](https://raw.githubusercontent.com/Sym-jay/Blot/main/assets/demo/demo.gif)

```bash
pip install blot-git
cd your-repo
blot index           # one-time, builds .git/blot.db
blot install-hook    # adds Blot to your pre-commit hook
blot why src/app.py:42   # which commit explains this line, skipping refactors
blot index --github  # optional: issue/PR titles and labels via the gh CLI
```

> **Status:** pre-alpha. All three checks work. Replayed on 500 recent commits each of Flask, requests, Express, Django and React, Blot warns on 0.6–10% of commits; see the bench results on GitHub. Please report false alarms.

Meet Sora the slime, read the docs, or contribute on GitHub: https://github.com/Sym-jay/Blot
