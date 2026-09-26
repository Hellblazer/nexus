# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-219 Phase 2 Step 2 (nexus-wauo1.17), mechanism 1: the persistent
credential snapshot script `tests/e2e/auth-login.sh` is deleted outright.

T2 `nexus_rdr/219-research-14` established the `oauthAccount` seed
(`tests/e2e/.claude-auth/claude.json`, the second half of what
auth-login.sh wrote) is not needed -- a bare `{"hasCompletedOnboarding":
true}` `.claude.json` authenticates in every tested launch shape -- so
there is nothing left for this script to do: every harness now launches
under `tests/e2e/lib/claude_credentials.py run --`, which never touches a
`.credentials.json` file at all (RDR-219 rule 2).

Only OPERATIONAL references matter here -- an instruction telling a user
to run the deleted script, or a doc/comment that assumes it still exists.
Historical incident narrative that explains, in the past tense, why the
shared picker (`claude_credentials.py`) exists is fine to keep (same
comment-exclusion convention `test_claude_credentials_single_source_lint.py`
uses): CHANGELOG.md entries, the RDR's own Existing Infrastructure Audit
and Key Discoveries sections, and the module/test docstrings that narrate
the nexus-galkv.19 husk incident are not checked here.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "tests" / "e2e" / "auth-login.sh"
AUTH_DIR = REPO_ROOT / "tests" / "e2e" / ".claude-auth"


def test_auth_login_script_is_deleted() -> None:
    assert not SCRIPT.exists(), f"{SCRIPT} still exists -- expected it deleted"


def test_cc_validation_readme_no_longer_instructs_running_it() -> None:
    """No operational reference remains -- a mention of the deleted
    snapshot path inside the README's own "History" section, describing
    what the harness used to do before RDR-219, is fine and expected (same
    comment-exclusion convention as the module docstring above)."""
    readme = (REPO_ROOT / "tests" / "cc-validation" / "README.md").read_text()
    assert "auth-login.sh" not in readme, (
        "tests/cc-validation/README.md still references the deleted "
        "tests/e2e/auth-login.sh"
    )
    probe_start = readme.index("### Fast verification")
    probe_end = readme.index("\n## tmux isolation", probe_start)
    probe_recipe = readme[probe_start:probe_end]
    assert ".credentials.json" not in probe_recipe, (
        "the MCP probe recipe still writes a .credentials.json file: "
        f"{probe_recipe}"
    )
    assert "claude_credentials.py" in probe_recipe and " run --" in probe_recipe, (
        "the MCP probe recipe no longer launches under claude_credentials.py "
        f"run --: {probe_recipe}"
    )
