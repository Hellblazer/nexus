# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-3jjkg: the RDR gate is keyed to the RDR file's blob, so a rebase that
leaves the text alone does not orphan the gate record, the fix-check title or
the accept check. Scenarios run against a real tmp git repo whose gated commit
is made unreachable the way a rebase before landing makes it."""
from __future__ import annotations

import subprocess

import pytest

from tests.test_rdr_preamble import (  # noqa: F401 — rdr_env and _rdr_git_template are fixtures
    _ROUND_BODY,
    _assert_output,
    _git_commit_rdr,
    _preamble,
    _rdr_git_template,
    _use_t2,
    rdr_env,
)

_RDR_REL = "docs/rdr/rdr-204-example.md"
_THREE_RUNS = "dispatches: 3\nFIX CHECK: PASS\nFIX CHECK: PASS\nFIX CHECK: PASS\n"


def _git_in(rdr_env, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(rdr_env["repo_root"]), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _blob_at_head(rdr_env) -> str:
    return _git_in(rdr_env, "rev-parse", "--short=12", f"HEAD:{_RDR_REL}")


def _rewrite_history_keeping_text(rdr_env, gated_commit: str) -> None:
    """What a rebase before landing does to a gated sha: the commit is
    replaced by one with the same tree, and the old one is gone."""
    _git_in(rdr_env, "commit", "-q", "--amend", "-m", "rebased onto develop")
    _git_in(rdr_env, "reflog", "expire", "--expire=now", "--all")
    _git_in(rdr_env, "gc", "-q", "--prune=now")
    gone = subprocess.run(
        ["git", "-C", str(rdr_env["repo_root"]), "cat-file", "-e", f"{gated_commit}^{{commit}}"],
        capture_output=True,
    )
    assert gone.returncode != 0, "the gated commit must be unreachable for this scenario to mean anything"


def _blob_gate(commit: str, blob: str, *, fix_check: str | None = None, prior: str | None = "[1] (BLOCKED 1C)") -> str:
    out = f'outcome: "PASSED"\ndate: "2026-09-07"\ncommit: {commit}\nblob: {blob}\n'
    if prior:
        out += f"prior: {prior}\n"
    if fix_check:
        out += f"fix_check: {fix_check}\n"
    return out


@pytest.mark.t2_tenant
def test_gate_keyed_to_a_blob_survives_a_rebase(rdr_env, monkeypatch):
    """The text the gate judged is unchanged, the commit that carried it is
    gone: the re-gate block reports no change, the fix-check pointer naming
    the blob is accepted, and accept admits the record."""
    import nexus.commands.rdr as rdr_mod

    c1 = _git_commit_rdr(rdr_env, _ROUND_BODY, "gated")
    blob = _blob_at_head(rdr_env)
    gate = _blob_gate(c1, blob, fix_check=f"nexus_rdr/204-fix-check-{blob}")
    entries = {"204-gate-latest": gate, f"204-fix-check-{blob}": _THREE_RUNS}
    _rewrite_history_keeping_text(rdr_env, c1)
    assert _blob_at_head(rdr_env) == blob

    _use_t2(monkeypatch, entries)
    out = _preamble("rdr-gate", "--", "204").output
    _assert_output(
        "blob_survives_rebase", out,
        has=["Fix check: not required", f"gated blob `{blob}`"],
        lacks=["does not resolve", "Fix check pointer mismatch", "Fix check record missing",
               "under-dispatched", "### Fix check (required"],
    )
    assert "Changed since the gated blob `%s`: no changes to the RDR file" % blob in out, out

    assert rdr_mod._gate_record_accept_defect(gate, "204-gate-latest") is None
    gate_value, note = rdr_mod._gate_outcome_for("204", "nexus")
    assert (gate_value, note) == ("passed", None)


@pytest.mark.t2_tenant
def test_an_edit_after_the_blob_gate_is_detected_and_names_the_new_blob(rdr_env, monkeypatch):
    c1 = _git_commit_rdr(rdr_env, _ROUND_BODY, "gated")
    gated_blob = _blob_at_head(rdr_env)
    _git_commit_rdr(rdr_env, _ROUND_BODY + "\nFive registration sites, not one.\n", "fix")
    new_blob = _blob_at_head(rdr_env)
    assert new_blob != gated_blob
    _use_t2(monkeypatch, {"204-gate-latest": _blob_gate(c1, gated_blob, prior=None)})
    out = _preamble("rdr-gate", "--", "204").output
    _assert_output(
        "blob_edit_detected", out,
        has=["### Fix check (required before Layer 1)", f"git diff {gated_blob} {new_blob}",
             f"204-fix-check-{new_blob}", "equal to its `blob:`", "1 file changed"],
        lacks=["Fix check: not required"],
    )
    # the fix commits are still listed while the gated commit resolves
    assert "Fix commits:" in out and "fix" in out


@pytest.mark.t2_tenant
def test_blob_keyed_fix_commits_say_so_when_the_gated_commit_is_gone(rdr_env, monkeypatch):
    c1 = _git_commit_rdr(rdr_env, _ROUND_BODY, "gated")
    gated_blob = _blob_at_head(rdr_env)
    _git_commit_rdr(rdr_env, _ROUND_BODY + "\nmore\n", "fix")
    # a rebase: the gated commit is replaced by an equivalent, the fix replayed on it
    _git_in(rdr_env, "reset", "-q", "--hard", "HEAD~1")
    _git_in(rdr_env, "commit", "-q", "--amend", "-m", "rebased")
    _git_commit_rdr(rdr_env, _ROUND_BODY + "\nmore\n", "fix replayed")
    _git_in(rdr_env, "reflog", "expire", "--expire=now", "--all")
    _git_in(rdr_env, "gc", "-q", "--prune=now")
    gone = subprocess.run(["git", "-C", str(rdr_env["repo_root"]), "cat-file", "-e", f"{c1}^{{commit}}"], capture_output=True)
    assert gone.returncode != 0, "the gated commit must be unreachable for this scenario to mean anything"
    _use_t2(monkeypatch, {"204-gate-latest": _blob_gate(c1, gated_blob, prior=None)})
    out = _preamble("rdr-gate", "--", "204").output
    assert "### Fix check (required before Layer 1)" in out, out
    assert "no longer resolves" in out, out


@pytest.mark.t2_tenant
def test_a_blob_record_rejects_a_fix_check_naming_the_commit(rdr_env, monkeypatch):
    """With `blob:` on the record the pointer must name the blob; the commit
    is no longer the key."""
    c1 = _git_commit_rdr(rdr_env, _ROUND_BODY, "gated")
    blob = _blob_at_head(rdr_env)
    gate = _blob_gate(c1, blob, fix_check=f"nexus_rdr/204-fix-check-{c1}")
    _use_t2(monkeypatch, {"204-gate-latest": gate, f"204-fix-check-{c1}": _THREE_RUNS})
    out = _preamble("rdr-gate", "--", "204").output
    assert "Fix check pointer mismatch" in out, out
    import nexus.commands.rdr as rdr_mod
    assert "older tree" in (rdr_mod._gate_record_accept_defect(gate, "204-gate-latest") or "")


@pytest.mark.t2_tenant
def test_a_commit_only_record_still_reports_an_orphaned_commit(rdr_env, monkeypatch):
    """Records written before nexus-3jjkg carry no blob: and behave as they
    always did, including saying so when the gated commit is gone."""
    c1 = _git_commit_rdr(rdr_env, _ROUND_BODY, "gated")
    _rewrite_history_keeping_text(rdr_env, c1)
    _use_t2(monkeypatch, {"204-gate-latest": f'outcome: "PASSED"\ndate: "2026-09-07"\ncommit: {c1}\n'})
    out = _preamble("rdr-gate", "--", "204").output
    assert f"Fix check: the gated commit `{c1}` does not resolve" in out, out
    assert "gated blob" not in out


@pytest.mark.t2_tenant
def test_rdr_fix_preamble_reads_the_blob_key(rdr_env, monkeypatch):
    c1 = _git_commit_rdr(rdr_env, _ROUND_BODY, "gated")
    gated_blob = _blob_at_head(rdr_env)
    _git_commit_rdr(rdr_env, _ROUND_BODY + "\nfixed\n", "fix one")
    new_blob = _blob_at_head(rdr_env)
    entries = {"204-gate-latest": f'outcome: "BLOCKED"\ncommit: {c1}\nblob: {gated_blob}\n'}
    _use_t2(monkeypatch, entries)
    out = _preamble("rdr-fix", "--", "204").output
    _assert_output(
        "rdr_fix_blob", out,
        has=[f"Gated blob `{gated_blob}`", f"git diff {gated_blob} {new_blob}",
             f"204-fix-check-{new_blob}", "fix one"],
        lacks=["carries no `commit:`"],
    )


@pytest.mark.t2_tenant
def test_the_verdict_block_writes_blob_and_keys_the_fix_check_and_history_line_to_it(rdr_env, monkeypatch):
    _git_commit_rdr(rdr_env, _ROUND_BODY, "gated")
    blob = _blob_at_head(rdr_env)
    crit = ("## Critical Issues\n\n### Issue: one thing\n- Location: a\n\n"
            "## Verdict\n- **outcome**: not-justified\n- **critical_count**: 1\n")
    _use_t2(monkeypatch, {
        "204-gate-critique-2026-09-12-r2": crit,
        "204-gate-latest": 'outcome: BLOCKED\ndate: 2026-09-10\ncommit: abc1234\n',
    })
    out = _preamble("rdr-verdict", "--", "204", "204-gate-critique-2026-09-12-r2").output
    assert f"blob: {blob}" in out, out
    assert f"commit: " in out
    assert f"204-fix-check-{blob}" in out, out
    assert f"blob `{blob}`" in out, out
