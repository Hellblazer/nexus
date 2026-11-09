"""The git-stash violation rule of the transcripts procedure (RDR-238 Phase 2 Step 1, nexus-qo7ie.12).

Only a stash that changes state counts (Sam, ask q12-stash-scope, 2026-11-09). These cases
moved out of test_procedure_transcripts.py under its file-size ceiling.
"""

from __future__ import annotations

import pytest

from tests.test_procedure_transcripts import DAY, STASH, call, rec, run


@pytest.mark.parametrize(
    "command,rule",
    [
        ("git stash", STASH),
        ("cd x && git stash pop", STASH),
        ("git stashed", None),
        ("git log stash", None),
        ("git -C stash status", None),
        ("echo 'git stash' > notes", None),
        ('echo "git stash"', None),
        ('git commit -m "do not git stash"', None),
        ("git commit -F - <<'EOF'\nuse git stash here\nEOF", None),
        # global options before the subcommand, a stash after a heredoc, a comment
        ("git -C /tmp/x stash", STASH),
        ("git --no-pager stash list", None),
        # only a stash that changes state counts (Sam, ask q12-stash-scope, 2026-11-09):
        # list, show and create touch neither refs/stash nor the working tree
        ("git stash list", None),
        ("git stash show -p stash@{0}", None),
        ("git -C /tmp/x stash show", None),
        ("git stash create", None),
        ("git stash list>/tmp/x", None),
        ("git stash pop>/tmp/x", STASH),
        ("git stash push -m wip", STASH),
        ("git stash save wip", STASH),
        ("git stash -u", STASH),
        ("git stash apply", STASH),
        ("git stash drop stash@{1}", STASH),
        ("git stash clear", STASH),
        ("git stash branch b", STASH),
        ("git stash store abc123", STASH),
        ("git stash list; git stash pop", STASH),
        ("git -c core.pager=cat stash pop", STASH),
        ("git --git-dir=.git stash", STASH),
        ("git --git-dir .git --work-tree . stash", STASH),
        ("cat <<EOF\nnotes\nEOF\ngit stash", STASH),
        ("git commit -F - <<'EOF'\nuse git stash\nEOF\ngit stash", STASH),
        ("# git stash", None),
        ("ls # git stash", None),
        ("ls;\n# git stash\nls", None),
        ("echo a#b; git stash", STASH),
    ],
)
def test_the_git_stash_rule(command: str, rule: str | None) -> None:
    s = rec("s", f"{DAY}T10:00:00Z", calls=[call("Bash", {"command": command})])
    out, _ = run([s], "violations", until="2026-11-04")
    assert {r["rule"] for r in out["rows"] if r["count"]} == ({rule} if rule else set())
