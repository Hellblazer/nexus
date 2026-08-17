# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the shared identity-drop / manifest-write-failure /
completion-refusal collector helpers in ``nexus.commands._helpers``
(nexus-7f5qj).

These four functions are the extraction target for the reset+check+raise
sequence that was independently duplicated in ``index_repo_cmd``
(commands/index.py) and ``dt.py``'s ``index_cmd`` (nexus-tp8yk D2), and
is now needed a third and fourth time by ``index_pdf_cmd`` / ``index_md_
cmd`` — the point CLAUDE.md's "extract only when repetition is proven"
is satisfied. Tested in isolation (collectors monkeypatched directly)
so the CLI-layer tests in test_index_cmd.py / test_commands_dt.py only
need to prove *wiring*, not re-derive this logic.
"""
from __future__ import annotations

import click
import pytest

from nexus.commands._helpers import (
    emit_identity_drop_summary,
    raise_identity_drop_exception,
    raise_identity_drop_exception_for_file,
    reset_identity_drop_collectors,
)


@pytest.fixture(autouse=True)
def _clean_collectors():
    """Every test starts and ends with the three collectors zeroed —
    they are process-global (nexus.mcp_infra module state), so a prior
    test's leftover drop must never leak into this one."""
    reset_identity_drop_collectors()
    yield
    reset_identity_drop_collectors()


def test_reset_identity_drop_collectors_zeroes_all_three():
    from nexus.mcp_infra import (
        _record_complete_refusal,
        _record_manifest_identity_drop,
        _record_manifest_write_failure,
        get_complete_refusals,
        get_manifest_identity_drops,
        get_manifest_write_failures,
    )

    _record_manifest_write_failure("1.2.3")
    _record_manifest_identity_drop("docs__x", 4)
    _record_complete_refusal("1.2.3")
    assert get_manifest_write_failures()
    assert get_manifest_identity_drops()
    assert get_complete_refusals()

    reset_identity_drop_collectors()

    assert get_manifest_write_failures() == []
    assert get_manifest_identity_drops() == []
    assert get_complete_refusals() == []


def test_reset_identity_drop_collectors_also_zeroes_the_sweep_collector():
    """nexus-39upx hazard 4: the sweep collector was added to the SAME
    reset function (not a parallel one) so every existing call site
    picks up sweep-skip visibility for free — this pins that it is
    actually reachable from here, not just defined."""
    from nexus.mcp_infra import _record_superseded_sweep_skip, get_superseded_sweep_stats

    _record_superseded_sweep_skip("1.2.3", "knowledge__x", "note_lookup_failed")
    assert get_superseded_sweep_stats()["skipped"]

    reset_identity_drop_collectors()

    assert get_superseded_sweep_stats() == {"swept": 0, "skipped": []}


def test_emit_identity_drop_summary_silent_and_false_when_all_empty(capsys):
    result = emit_identity_drop_summary(indexed_count=1)
    assert result is False
    captured = capsys.readouterr()
    assert captured.err == ""


def test_emit_identity_drop_summary_surfaces_write_failures(capsys):
    from nexus.mcp_infra import _record_manifest_write_failure

    _record_manifest_write_failure("1.2.3")
    _record_manifest_write_failure("1.2.4")

    result = emit_identity_drop_summary(indexed_count=2)

    assert result is True
    err = capsys.readouterr().err
    assert "WARNING: catalog manifest write failed for 2 document(s)" in err
    assert "nx catalog reconcile" in err


def test_emit_identity_drop_summary_surfaces_identity_drops(capsys):
    from nexus.mcp_infra import _record_manifest_identity_drop

    _record_manifest_identity_drop("rdr__nexus", 23)
    _record_manifest_identity_drop("rdr__nexus", 4)

    result = emit_identity_drop_summary(indexed_count=2)

    assert result is True
    err = capsys.readouterr().err
    assert (
        "WARNING: 2 chunk batch(es) (27 chunks; collection(s): rdr__nexus) "
        "were indexed WITHOUT a catalog document identity" in err
    )
    assert "nx catalog reconcile" in err


def test_emit_identity_drop_summary_surfaces_complete_refusals(capsys):
    from nexus.mcp_infra import _record_complete_refusal

    _record_complete_refusal("1.2.3")

    result = emit_identity_drop_summary(indexed_count=5)

    assert result is True
    err = capsys.readouterr().err
    assert (
        "WARNING: 1 of the 5 indexed above had completion refused" in err
    )
    assert "Re-index or --force to retry" in err


def test_emit_identity_drop_summary_surfaces_swept_count_as_info_not_a_problem(capsys):
    """nexus-39upx hazard 4: a successful sweep is good news — it must be
    visible (capability-honest) but must NOT trip the batch fail-loud
    gate the way write-failures/drops/refusals do."""
    from nexus.mcp_infra import _record_superseded_swept

    _record_superseded_swept(3)

    result = emit_identity_drop_summary(indexed_count=2)

    assert result is False
    out = capsys.readouterr()
    assert "swept 3 superseded T3 chunk(s)" in out.out
    assert out.err == ""


def test_emit_identity_drop_summary_surfaces_sweep_skips_as_a_problem(capsys):
    """nexus-39upx hazard 4: a sweep that could not run leaves old,
    superseded T3 rows searchable — the exact silent-orphan shape this
    bead exists to close one layer up. Must count toward the non-zero
    exit like the other three collectors (uqq9z/7f5qj strictness)."""
    from nexus.mcp_infra import _record_superseded_sweep_skip

    _record_superseded_sweep_skip("1.2.3", "knowledge__x", "note_lookup_failed")

    result = emit_identity_drop_summary(indexed_count=2)

    assert result is True
    err = capsys.readouterr().err
    assert "WARNING: superseded-chunk sweep skipped for 1 document(s)" in err
    assert "note_lookup_failed" in err
    assert "nx t3 gc -c COLLECTION" in err


def test_emit_identity_drop_summary_write_failure_noun_is_hardcoded_document(capsys):
    """nexus-7f5qj: verified pre-extraction that BOTH existing call sites
    (index_repo_cmd, dt.py's index_cmd) hardcode "document(s)" for this
    specific line — dt.py otherwise says "record(s)" everywhere else in
    its own summary, but not here. A per-caller noun parameter would
    silently change dt.py's real output; this pins the hardcode."""
    from nexus.mcp_infra import _record_manifest_write_failure

    _record_manifest_write_failure("1.2.3")

    emit_identity_drop_summary(indexed_count=1)

    err = capsys.readouterr().err
    assert "catalog manifest write failed for 1 document(s)" in err


def test_emit_identity_drop_summary_default_order_matches_index_repo_cmd(capsys):
    """Default order (no explicit ``order=``) must be write_failed ->
    identity_drops -> refused — index_repo_cmd's pre-extraction sequence,
    also used fresh by index_pdf_cmd / index_md_cmd."""
    from nexus.mcp_infra import (
        _record_complete_refusal,
        _record_manifest_identity_drop,
        _record_manifest_write_failure,
    )

    _record_manifest_write_failure("1.2.3")
    _record_manifest_identity_drop("docs__x", 3)
    _record_complete_refusal("1.2.3")

    emit_identity_drop_summary(indexed_count=5)

    err = capsys.readouterr().err
    idx_write = err.find("catalog manifest write failed")
    idx_drop = err.find("WITHOUT a catalog document identity")
    idx_refused = err.find("completion refused")
    assert -1 not in (idx_write, idx_drop, idx_refused)
    assert idx_write < idx_drop < idx_refused


def test_emit_identity_drop_summary_order_param_controls_print_sequence(capsys):
    """nexus-7f5qj code-review follow-up (T2 [21484]): dt.py's index_cmd
    passes order=("refused", "write_failed", "identity_drops") to
    preserve ITS pre-extraction print sequence exactly (a behavior-
    preserving refactor should not silently reorder WARNING lines even
    when no test happened to pin the order)."""
    from nexus.mcp_infra import (
        _record_complete_refusal,
        _record_manifest_identity_drop,
        _record_manifest_write_failure,
    )

    _record_manifest_write_failure("1.2.3")
    _record_manifest_identity_drop("docs__x", 3)
    _record_complete_refusal("1.2.3")

    emit_identity_drop_summary(
        indexed_count=5, order=("refused", "write_failed", "identity_drops"),
    )

    err = capsys.readouterr().err
    idx_refused = err.find("completion refused")
    idx_write = err.find("catalog manifest write failed")
    idx_drop = err.find("WITHOUT a catalog document identity")
    assert -1 not in (idx_refused, idx_write, idx_drop)
    assert idx_refused < idx_write < idx_drop


def test_raise_identity_drop_exception_default_wording_matches_index_repo_cmd():
    with pytest.raises(click.ClickException) as exc_info:
        raise_identity_drop_exception(subject="document")
    msg = str(exc_info.value)
    assert msg == (
        "one or more documents had manifest write failures, "
        "identity drops, or completion refusals this run — see "
        "the WARNING lines above. Run 'nx catalog show <tumbler>' to "
        "inspect a specific document's index_state, or re-index "
        "with --force."
    )


def test_raise_identity_drop_exception_record_wording_matches_dt_index_cmd():
    with pytest.raises(click.ClickException) as exc_info:
        raise_identity_drop_exception(subject="record")
    msg = str(exc_info.value)
    assert msg == (
        "one or more records had manifest write failures, "
        "identity drops, or completion refusals this run — see "
        "the WARNING lines above. Run 'nx catalog show <tumbler>' to "
        "inspect a specific record's index_state, or re-index "
        "with --force."
    )


# ── nexus-39upx round 2 SIGNIFICANT 2: per-cause accurate remedy ─────────
#
# The original message named ALL THREE write-class causes and the
# manifest-verify remedy UNCONDITIONALLY, regardless of which one(s)
# actually fired. Adding a fourth, housekeeping-only trigger
# (superseded_sweep_skipped) to the same fail-loud gate made that
# inaccurate for the new class: a run tripping ONLY the sweep skip would
# exit non-zero pointing an operator at manifest-verify for a document
# whose manifest write never failed. (RDR-191 Phase 6, nexus-o8dil.33:
# `nx catalog manifest-verify` itself is now retired; the write-class
# remedy points at `nx catalog show <tumbler>` instead — the per-cause
# accuracy this section describes is unaffected by that rewording.)


def test_raise_identity_drop_exception_sweep_skip_only_names_the_right_cause_and_remedy():
    from nexus.mcp_infra import _record_superseded_sweep_skip

    _record_superseded_sweep_skip("1.2.3", "knowledge__x", "note_lookup_failed")

    with pytest.raises(click.ClickException) as exc_info:
        raise_identity_drop_exception(subject="document")
    msg = str(exc_info.value)

    assert "a superseded-chunk sweep skip" in msg
    assert "nx t3 gc -c COLLECTION" in msg
    # Must NOT point at the write-class remedy — nothing wrote failed.
    assert "manifest-verify" not in msg
    assert "manifest write failures" not in msg
    assert "identity drops" not in msg
    assert "completion refusals" not in msg


def test_raise_identity_drop_exception_write_failure_only_does_not_mention_sweep():
    from nexus.mcp_infra import _record_manifest_write_failure

    _record_manifest_write_failure("1.2.3")

    with pytest.raises(click.ClickException) as exc_info:
        raise_identity_drop_exception(subject="document")
    msg = str(exc_info.value)

    assert "manifest write failures" in msg
    assert "nx catalog show" in msg
    assert "t3 gc" not in msg
    assert "sweep" not in msg


def test_raise_identity_drop_exception_mixed_cause_names_both_remedies():
    from nexus.mcp_infra import _record_manifest_write_failure, _record_superseded_sweep_skip

    _record_manifest_write_failure("1.2.3")
    _record_superseded_sweep_skip("1.2.3", "knowledge__x", "delete_failed")

    with pytest.raises(click.ClickException) as exc_info:
        raise_identity_drop_exception(subject="document")
    msg = str(exc_info.value)

    assert "manifest write failures" in msg
    assert "a superseded-chunk sweep skip" in msg
    assert "nx catalog show" in msg
    assert "t3 gc -c COLLECTION" in msg


def test_raise_identity_drop_exception_for_file_names_file_and_remedy(tmp_path):
    target = tmp_path / "orphan.pdf"
    with pytest.raises(click.ClickException) as exc_info:
        raise_identity_drop_exception_for_file(target, chunks=7)
    msg = str(exc_info.value)
    assert str(target) in msg
    assert "7 chunk" in msg
    assert "orphaned" in msg.lower()
    assert "reconcile" in msg.lower()
    # remedy: re-run once reachable, chunks reconcile via upsert identity
    assert "re-run" in msg.lower() or "reindex" in msg.lower() or "re-index" in msg.lower()
