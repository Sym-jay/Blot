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
```

> **Status:** pre-alpha. All three checks work; expect rough edges and please report false alarms.

Meet Sora the slime, read the docs, or contribute on GitHub: https://github.com/Sym-jay/Blot
