# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-182 P2.2 (nexus-ykzbj.9): pre-emission read-only SQL lint.

Guarantees any DIAGNOSTIC SQL is read-only and metadata-scoped BEFORE it
ever reaches an agent — mutating diagnostics are impossible-by-construction.
Fail-closed allowlist, not a deny-list: a statement passes only when it
provably matches a read-only shape; anything unrecognized fails. (The audit
note is explicit: the test-suite's leading-keyword ``_DML_TARGET_RE``
pattern is NOT sufficient — CTEs and DO blocks must be caught.)

The RDR-182 diagnostic-playbook EMITTER this lint was wired into is
deleted (nexus-lgdel — the chash-rekey rung it steered operators toward no
longer exists); ``sql_lint`` itself is general-purpose and stays, consumed
directly by ``nexus.db.diag_connection`` and ``nexus.health``.
"""
from __future__ import annotations

import pytest


def _lint():
    from nexus.remediation import sql_lint

    return sql_lint


# ── mutating statements: every one FAILS ────────────────────────────────────

_MUTATING = [
    "INSERT INTO nexus.memory (id) VALUES (1)",
    "UPDATE nexus.chunks SET chash = 'x'",
    "DELETE FROM nexus.documents WHERE 1=1",
    "DROP TABLE nexus.chunks",
    "ALTER TABLE nexus.chunks DROP CONSTRAINT chk_chash_len",
    "TRUNCATE nexus.topic_assignments",
    "CREATE TABLE nexus.evil (id int)",
    "GRANT ALL ON nexus.memory TO PUBLIC",
    "REVOKE SELECT ON nexus.memory FROM nexus_diag",
    "MERGE INTO nexus.memory USING nexus.plans ON true WHEN MATCHED THEN DELETE",
    # CTE smuggling a data-modifying statement (the audit-note case):
    "WITH gone AS (DELETE FROM nexus.documents RETURNING id) SELECT count(*) FROM gone",
    # DO block (arbitrary plpgsql):
    "DO $$ BEGIN DELETE FROM nexus.memory; END $$",
    # Procedure call:
    "CALL nexus.some_proc()",
    # SELECT ... INTO creates a table:
    "SELECT * INTO nexus.copy_of_memory FROM nexus.memory",
    # Locking reads mutate lock state:
    "SELECT * FROM nexus.memory FOR UPDATE",
]


@pytest.mark.parametrize("stmt", _MUTATING)
def test_mutating_statement_fails(stmt):
    ok, reason = _lint().is_read_only_diagnostic(stmt)
    assert ok is False
    assert reason  # a violation always carries a reason


# ── read-only metadata diagnostics: PASS ────────────────────────────────────

_READ_ONLY = [
    "SELECT id, filename FROM public.databasechangelog ORDER BY orderexecuted",
    "SELECT conname, convalidated FROM pg_constraint WHERE conname LIKE 'chk_%'",
    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'nexus'",
    "SELECT relname, reltuples FROM pg_catalog.pg_class",
    # counts over store tables are allowed — aggregate-only select list:
    "SELECT count(*) FROM nexus.chunks WHERE length(chash) <> 32",
    "SELECT COUNT(*), MIN(length(chash)), MAX(length(chash)) FROM nexus.chunks",
    # CTE composed purely of SELECTs:
    "WITH bad AS (SELECT count(*) AS n FROM nexus.chunks WHERE length(chash) <> 32) SELECT n FROM bad",
]


@pytest.mark.parametrize("stmt", _READ_ONLY)
def test_read_only_metadata_statement_passes(stmt):
    ok, reason = _lint().is_read_only_diagnostic(stmt)
    assert ok is True, reason


# ── content protection: SELECTing row/document/note CONTENT fails ──────────

_CONTENT_LEAKS = [
    # non-aggregate select over a store table pulls row content:
    "SELECT content FROM nexus.memory",
    "SELECT * FROM nexus.chunks",
    "SELECT chash, document FROM nexus.chunks LIMIT 5",
    "SELECT title, content FROM t1.scratch",
]


@pytest.mark.parametrize("stmt", _CONTENT_LEAKS)
def test_store_content_reference_fails(stmt):
    ok, reason = _lint().is_read_only_diagnostic(stmt)
    assert ok is False
    assert "content" in reason.lower() or "aggregate" in reason.lower()


# ── fail-closed on UNQUALIFIED / unknown targets (critic-final M1) ──────────

_UNQUALIFIED_LEAKS = [
    # No schema prefix — must NOT slip past just because it lacks 'nexus.'
    "SELECT content FROM chunks",
    "SELECT * FROM memory",
    "SELECT chash, document FROM chunks LIMIT 5",
    # Unknown/unqualified target with a non-aggregate projection:
    "SELECT title FROM some_future_table",
]


@pytest.mark.parametrize("stmt", _UNQUALIFIED_LEAKS)
def test_unqualified_unknown_target_fails_closed(stmt):
    ok, reason = _lint().is_read_only_diagnostic(stmt)
    assert ok is False, f"unqualified content read slipped past: {stmt}"
    assert "content" in reason.lower() or "aggregate" in reason.lower()


_UNQUALIFIED_OK = [
    # Aggregate-only over an unqualified target is still fine (count-safe).
    "SELECT count(*) FROM chunks WHERE length(chash) <> 32",
    # Bare catalog objects stay unrestricted.
    "SELECT conname FROM pg_constraint WHERE conname LIKE 'chk_%'",
    "SELECT relname FROM pg_class",
    # CTE name (unqualified) whose body is aggregate-only.
    "WITH bad AS (SELECT count(*) AS n FROM chunks) SELECT n FROM bad",
]


@pytest.mark.parametrize("stmt", _UNQUALIFIED_OK)
def test_unqualified_but_safe_targets_pass(stmt):
    ok, reason = _lint().is_read_only_diagnostic(stmt)
    assert ok is True, reason


# ── the batch assertion ──────────────────────────────────────────────────────

def test_assert_batch_raises_on_first_violation():
    lint = _lint()
    with pytest.raises(lint.DiagnosticSqlViolation) as exc:
        lint.assert_read_only_diagnostics([
            "SELECT count(*) FROM nexus.chunks",
            "DELETE FROM nexus.memory",
        ])
    assert "DELETE FROM nexus.memory" in str(exc.value)


def test_empty_batch_is_fine():
    _lint().assert_read_only_diagnostics([])
