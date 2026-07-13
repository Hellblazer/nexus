# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-182 P2.2 (nexus-ykzbj.9): pre-emission read-only SQL lint.

Guarantees any SQL a DIAGNOSTIC (forensics) playbook emits is read-only and
metadata-scoped BEFORE it ever reaches an agent — mutating diagnostics are
impossible-by-construction. Fail-closed allowlist, not a deny-list: a
statement passes only when it provably matches a read-only shape; anything
unrecognized fails. (The audit note is explicit: the test-suite's
leading-keyword ``_DML_TARGET_RE`` pattern is NOT sufficient — CTEs and DO
blocks must be caught.)
"""
from __future__ import annotations

import pytest


def _lint():
    from nexus.remediation import sql_lint

    return sql_lint


# ── mutating statements: every one FAILS ────────────────────────────────────

_MUTATING = [
    "INSERT INTO nexus.memory (id) VALUES (1)",
    "UPDATE nexus.chunks_768 SET chash = 'x'",
    "DELETE FROM nexus.documents WHERE 1=1",
    "DROP TABLE nexus.chunks_768",
    "ALTER TABLE nexus.chunks_768 DROP CONSTRAINT chk_chash_len",
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
    "SELECT count(*) FROM nexus.chunks_768 WHERE length(chash) <> 32",
    "SELECT COUNT(*), MIN(length(chash)), MAX(length(chash)) FROM nexus.chunks_768",
    # CTE composed purely of SELECTs:
    "WITH bad AS (SELECT count(*) AS n FROM nexus.chunks_768 WHERE length(chash) <> 32) SELECT n FROM bad",
]


@pytest.mark.parametrize("stmt", _READ_ONLY)
def test_read_only_metadata_statement_passes(stmt):
    ok, reason = _lint().is_read_only_diagnostic(stmt)
    assert ok is True, reason


# ── content protection: SELECTing row/document/note CONTENT fails ──────────

_CONTENT_LEAKS = [
    # non-aggregate select over a store table pulls row content:
    "SELECT content FROM nexus.memory",
    "SELECT * FROM nexus.chunks_768",
    "SELECT chash, document FROM nexus.chunks_768 LIMIT 5",
    "SELECT title, content FROM t1.scratch",
]


@pytest.mark.parametrize("stmt", _CONTENT_LEAKS)
def test_store_content_reference_fails(stmt):
    ok, reason = _lint().is_read_only_diagnostic(stmt)
    assert ok is False
    assert "content" in reason.lower() or "aggregate" in reason.lower()


# ── the batch assertion + emitter wiring ────────────────────────────────────

def test_assert_batch_raises_on_first_violation():
    lint = _lint()
    with pytest.raises(lint.DiagnosticSqlViolation) as exc:
        lint.assert_read_only_diagnostics([
            "SELECT count(*) FROM nexus.chunks_768",
            "DELETE FROM nexus.memory",
        ])
    assert "DELETE FROM nexus.memory" in str(exc.value)


def test_empty_batch_is_fine():
    _lint().assert_read_only_diagnostics([])


def test_emitter_refuses_a_diagnostic_playbook_with_mutating_sql(monkeypatch):
    """Wired into the emitter path: a (hypothetical future) diagnostic topic
    whose builder embeds mutating SQL cannot be emitted at all."""
    from nexus.remediation import StoreState, emit_playbook, sql_lint
    from nexus.remediation import playbook as pb_mod

    def _evil_topic(store_state):
        pb = pb_mod._chash_poison(store_state)
        object.__setattr__(pb, "diagnostic_sql",
                           ("DELETE FROM nexus.memory",))
        return pb

    monkeypatch.setitem(pb_mod._TOPICS, "evil-diag", _evil_topic)
    with pytest.raises(sql_lint.DiagnosticSqlViolation):
        emit_playbook("evil-diag", StoreState(detail="x"))


def test_emitter_passes_a_clean_diagnostic_playbook(monkeypatch):
    from nexus.remediation import StoreState, emit_playbook
    from nexus.remediation import playbook as pb_mod

    def _clean_topic(store_state):
        pb = pb_mod._chash_poison(store_state)
        object.__setattr__(pb, "diagnostic_sql",
                           ("SELECT count(*) FROM nexus.chunks_768 WHERE length(chash) <> 32",))
        return pb

    monkeypatch.setitem(pb_mod._TOPICS, "clean-diag", _clean_topic)
    pb = emit_playbook("clean-diag", StoreState(detail="x"))
    assert pb.diagnostic_sql


def test_diagnostic_sql_renders_in_tool_return(monkeypatch):
    """(review-foundations Medium) Linted SQL the agent never SEES is a
    silent gap: a populated diagnostic_sql must appear verbatim in the MCP
    rendering — and stay OUT of the CLI/agent-prompt renderings."""
    from nexus.remediation import StoreState, emit_playbook
    from nexus.remediation import playbook as pb_mod

    stmt = "SELECT count(*) FROM nexus.chunks_768 WHERE length(chash) <> 32"

    def _clean_topic(store_state):
        pb = pb_mod._chash_poison(store_state)
        object.__setattr__(pb, "diagnostic_sql", (stmt,))
        return pb

    monkeypatch.setitem(pb_mod._TOPICS, "clean-diag", _clean_topic)
    pb = emit_playbook("clean-diag", StoreState(detail="x"))
    assert stmt in pb.tool_return()
    assert "lint-verified" in pb.tool_return()
    assert stmt not in pb.agent_prompt()
    assert stmt not in pb.terminal_block()
    # And the empty-default case renders NO sql block at all.
    plain = emit_playbook("chash-poison", StoreState(detail="x"))
    assert "diagnostic SQL" not in plain.tool_return()
