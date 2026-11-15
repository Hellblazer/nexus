# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-3jjkg: the RDR gate is keyed to the RDR file's blob, so a rebase that
leaves the text alone does not orphan the gate record, the fix-check title or
the accept check. Scenarios run against a real tmp git repo whose gated commit
is made unreachable the way a rebase before landing makes it."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_rdr_preamble import (  # noqa: F401 — _rdr_git_template is a fixture
    _ROUND_BODY,
    _assert_output,
    _git_commit_rdr,
    _preamble,
    _rdr_git_template,
    _use_t2,
)

_RDR_REL = "docs/rdr/rdr-204-example.md"


@pytest.fixture()
def rdr_env(tmp_path: Path, monkeypatch, _rdr_git_template: Path):
    """The test_rdr_preamble ``rdr_env`` without its live T2 database. These
    tests are about the RDR file's blob and the gate record's shape, and every
    record they read comes from the in-memory double ``_use_t2`` installs, so
    they ask for no tenant (storage-touch ceiling, tests/storage_touch_ceiling.toml)."""
    shutil.copytree(_rdr_git_template / ".git", tmp_path / ".git")
    monkeypatch.chdir(tmp_path)
    rdr_dir = tmp_path / "docs" / "rdr"
    rdr_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("nexus.commands._helpers.default_db_path", lambda: tmp_path / "t2.db")
    return {"rdr_dir": rdr_dir, "repo_root": tmp_path}
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


def test_blob_keyed_fix_commits_survive_a_gc_of_the_gated_commit(rdr_env, monkeypatch):
    """After gc the gated commit is gone; the listing walks the file's own
    history to the gated blob and never needed it."""
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
    assert "- " in out.split("Fix commits:", 1)[1] and "fix replayed" in out, out
    assert "no longer resolves" not in out, out


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


def test_a_commit_only_record_still_reports_an_orphaned_commit(rdr_env, monkeypatch):
    """Records written before nexus-3jjkg carry no blob: and behave as they
    always did, including saying so when the gated commit is gone."""
    c1 = _git_commit_rdr(rdr_env, _ROUND_BODY, "gated")
    _rewrite_history_keeping_text(rdr_env, c1)
    _use_t2(monkeypatch, {"204-gate-latest": f'outcome: "PASSED"\ndate: "2026-09-07"\ncommit: {c1}\n'})
    out = _preamble("rdr-gate", "--", "204").output
    assert f"Fix check: the gated commit `{c1}` does not resolve" in out, out
    assert "gated blob" not in out


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


# ---------------------------------------------------------------------------
# round 1 review (ef27a76c4): the transition, the dirty file, the fix-commit walk
# ---------------------------------------------------------------------------

_CRIT = (
    "## Critical Issues\n\n### Issue: one thing\n- Location: a\n\n"
    "## Verdict\n- **outcome**: not-justified\n- **critical_count**: 1\n"
)
_VERDICT_ARGS = ("rdr-verdict", "--", "204", "204-gate-critique-2026-09-12-r2")


def _title_sha(out: str) -> str:
    import re

    m = re.search(r"204-fix-check-([0-9a-f]{6,40})", out)
    assert m, out
    return m.group(1)


def test_a_commit_only_record_hands_off_to_a_blob_keyed_next_round(rdr_env, monkeypatch):
    """Review item 1: the gate record being re-gated has only `commit:`, so the
    re-gate preamble titled the fix check by the tip COMMIT while the verdict
    printer wrote `blob:` and a blob `fix_check:`, and accept refused the pair.
    Both now name the file's current blob, and accept admits the record."""
    import nexus.commands.rdr as rdr_mod

    c1 = _git_commit_rdr(rdr_env, _ROUND_BODY, "gated")
    _git_commit_rdr(rdr_env, _ROUND_BODY + "\nFive registration sites, not one.\n", "fix")
    blob = _blob_at_head(rdr_env)
    tip_commit = _git_in(rdr_env, "log", "-1", "--format=%h", "--", _RDR_REL)
    old = f'outcome: "BLOCKED"\ndate: "2026-09-07"\ncommit: {c1}\n'
    _use_t2(monkeypatch, {"204-gate-latest": old, "204-gate-critique-2026-09-12-r2": _CRIT})

    gate_out = _preamble("rdr-gate", "--", "204").output
    assert "### Fix check (required before Layer 1)" in gate_out, gate_out
    assert _title_sha(gate_out) == blob, gate_out
    assert tip_commit not in gate_out.split("### Fix check", 1)[1].split("Range:", 1)[0]
    assert "equal to its `blob:`" in gate_out

    verdict_out = _preamble(*_VERDICT_ARGS).output
    assert f"blob: {blob}" in verdict_out, verdict_out
    assert _title_sha(verdict_out) == blob

    new = _blob_gate(c1, blob, fix_check=f"nexus_rdr/204-fix-check-{_title_sha(gate_out)}")
    assert rdr_mod._gate_record_accept_defect(new, "204-gate-latest") is None
    _use_t2(monkeypatch, {"204-gate-latest": new, f"204-fix-check-{blob}": _THREE_RUNS})
    assert rdr_mod._gate_outcome_for("204", "nexus") == ("passed", None)


def test_rdr_fix_preamble_titles_a_commit_only_record_by_the_blob(rdr_env, monkeypatch):
    c1 = _git_commit_rdr(rdr_env, _ROUND_BODY, "gated")
    _git_commit_rdr(rdr_env, _ROUND_BODY + "\nfixed\n", "fix one")
    blob = _blob_at_head(rdr_env)
    _use_t2(monkeypatch, {"204-gate-latest": f'outcome: "BLOCKED"\ncommit: {c1}\n'})
    out = _preamble("rdr-fix", "--", "204").output
    assert f"204-fix-check-{blob}" in out, out
    assert f"git diff {c1}..HEAD -- {_RDR_REL}" in out, out  # the prior key still picks the range


def test_a_dirty_rdr_file_leaves_the_blob_unfilled_and_says_so(rdr_env, monkeypatch):
    """Review item 2: HEAD:<rel> is not what the critic read when the working
    file has uncommitted edits."""
    _git_commit_rdr(rdr_env, _ROUND_BODY, "gated")
    head_blob = _blob_at_head(rdr_env)
    path = rdr_env["rdr_dir"] / "rdr-204-example.md"
    _use_t2(monkeypatch, {"204-gate-critique-2026-09-12-r2": _CRIT})

    clean = _preamble(*_VERDICT_ARGS).output
    assert f"blob: {head_blob}" in clean and "uncommitted edits" not in clean, clean
    assert "uncommitted edits" not in _preamble("rdr-gate", "--", "204").output

    path.write_text(path.read_text() + "\nan edit nobody committed\n")
    for argv in (_VERDICT_ARGS, ("rdr-gate", "--", "204")):
        out = _preamble(*argv).output
        assert "RDR file has uncommitted edits; commit before gating" in out, out
    out = _preamble(*_VERDICT_ARGS).output
    assert head_blob not in out, "the dirty file must not be keyed to HEAD's blob"
    assert "blob: <git rev-parse" in out, out


def test_fix_commits_skip_the_rebased_copy_of_the_gated_commit_without_gc(rdr_env, monkeypatch):
    """Review item 4: before gc the old gated commit still resolves, and
    `<old commit>..HEAD` listed its rebased twin as a fix commit."""
    c1 = _git_commit_rdr(rdr_env, _ROUND_BODY, "gated")
    gated_blob = _blob_at_head(rdr_env)
    _git_commit_rdr(rdr_env, _ROUND_BODY + "\nmore\n", "fix")
    _git_in(rdr_env, "reset", "-q", "--hard", "HEAD~1")
    _git_in(rdr_env, "commit", "-q", "--amend", "-m", "rebased copy of the gated commit")
    _git_commit_rdr(rdr_env, _ROUND_BODY + "\nmore\n", "fix replayed")
    _git_in(rdr_env, "cat-file", "-e", f"{c1}^{{commit}}")  # still resolves: no gc ran
    _use_t2(monkeypatch, {"204-gate-latest": _blob_gate(c1, gated_blob, prior=None)})
    out = _preamble("rdr-gate", "--", "204").output
    listing = out.split("Fix commits:", 1)[1].split("Dispatch substantive-critic", 1)[0]
    assert "fix replayed" in listing, out
    assert "rebased copy" not in listing and " gated" not in listing, listing


def test_a_gated_blob_absent_from_the_files_history_is_said(rdr_env, monkeypatch):
    c1 = _git_commit_rdr(rdr_env, _ROUND_BODY, "gated")
    _git_commit_rdr(rdr_env, _ROUND_BODY + "\nmore\n", "fix")
    _use_t2(monkeypatch, {"204-gate-latest": _blob_gate(c1, "deadbeef0123", prior=None)})
    out = _preamble("rdr-gate", "--", "204").output
    assert "was not found in this file's history" in out, out
    assert "re-gate" in out, out


def test_no_procedure_text_still_titles_a_commit_only_fix_check_by_the_tip_commit():
    """Round-2 critique of ee9e24429: the fix-check title is the file blob for
    every record, so no procedure text may still send a commit-only record's
    fix check to the tip commit, and no text may cite a commit "the critique
    names" (critique records carry no commit)."""
    root = Path(__file__).resolve().parents[1]
    sites = [
        root / "src" / "nexus" / "commands" / "rdr.py",
        *sorted((root / "conexus" / "skills").glob("rdr-*-checklist/SKILL.md")),
        *sorted((root / "conexus" / "commands").glob("rdr-*.md")),
    ]
    assert len(sites) >= 4, sites
    stale = (
        "tip commit for a record with only",
        "its tip commit, for a record with no",
        "the commit the critique names",
    )
    hits = [(p.name, s) for p in sites for s in stale if s in p.read_text(encoding="utf-8")]
    assert hits == []
