"""action.yml: the step that turns `blot check --format json` output into workflow annotations."""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from blot.cli import main

ACTION = Path(__file__).resolve().parent.parent / "action.yml"


def _annotate_script() -> str:
    """The Python heredoc in action.yml's "Run Blot" step (stdlib only, so no YAML parser needed)."""
    lines = ACTION.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "python - <<'PY'") + 1
    end = next(i for i in range(start, len(lines)) if lines[i].strip() == "PY")
    return textwrap.dedent("\n".join(lines[start:end])) + "\n"


def _annotate(tmp_path: Path, raw: str) -> list[str]:
    findings = tmp_path / "findings.json"
    findings.write_text(raw, encoding="utf-8")
    out = subprocess.run([sys.executable, "-c", _annotate_script()], env={**os.environ, "BLOT_FINDINGS": str(findings)},
                         capture_output=True, text=True, check=True).stdout
    return out.splitlines()


def test_findings_become_warning_annotations(tmp_path):
    findings = [
        {"check": "removed-fix", "path": "src/a,b.py", "line": 12, "severity": 2,
         "message": "You're changing a line added by a bug fix: 100% sure.",
         "evidence": ['8efea0cc "Break reference cycle" (2020-10-03, fixes #3761)']},
        {"check": "forgot-related-file", "path": "app.py", "line": None, "severity": 1,
         "message": "app.py usually changes with views.py.", "evidence": []},
    ]
    assert _annotate(tmp_path, json.dumps(findings)) == [
        "::warning file=src/a%2Cb.py,line=12,title=blot%3A removed-fix::"
        "You're changing a line added by a bug fix: 100%25 sure."
        '%0A8efea0cc "Break reference cycle" (2020-10-03, fixes #3761)',
        "::warning file=app.py,title=blot%3A forgot-related-file::app.py usually changes with views.py.",
        "blot: 2 findings",
    ]


def test_no_findings_and_no_output(tmp_path):
    assert _annotate(tmp_path, "[]\n") == ["blot: 0 findings"]
    assert _annotate(tmp_path, "") == ["blot: 0 findings"]  # blot check hit an internal error


def test_range_check_of_a_pr_branch_is_annotated(make_repo, tmp_path, capsys):
    from test_checks import AFTER, BEFORE, GUARD

    repo = make_repo([("Add app", {"app.py": BEFORE}),
                      ("Break reference cycle in App (fixes #3761)", {"app.py": AFTER})])
    git = lambda *args: subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)  # noqa: E731
    git("switch", "-q", "-c", "pr")
    (repo / "app.py").write_text(AFTER.replace(GUARD, "self_ref = self"), encoding="utf-8")
    git("commit", "-q", "-am", "Simplify App")

    assert main(["--repo", str(repo), "check", "--range", "main..HEAD", "--format", "json"]) == 0
    lines = _annotate(tmp_path, capsys.readouterr().out)
    assert lines[-1].startswith("blot: ") and lines[-1] != "blot: 0 findings"
    assert any(line.startswith("::warning file=app.py,line=") and ",title=blot%3A removed-fix::" in line
               for line in lines)
