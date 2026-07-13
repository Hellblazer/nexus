# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-182 P3.1 (nexus-ykzbj.10): the ``forensics(topic)`` MCP tool —
enabled-path behavior. (Gate behavior lives in test_rdr182_opt_in_gate.py.)

Contract: gate first (tested elsewhere) → resolve nexus_diag credentials →
run the topic's LINTED diagnostic SQL via the sanctioned choke point → embed
the live results as the playbook's store_detail → return tool_return() (the
only reliably-visible Desktop channel). Credentials absent → the playbook
still emits, with an explicit diagnostics-unavailable detail (degrade
cleanly, never crash, never silently claim clean). No outbound HTTP anywhere.
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def enabled_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg))
    monkeypatch.chdir(tmp_path)
    (cfg / "config.yml").write_text(
        "claude_assisted_remediation:\n  enabled: true\n"
    )
    return cfg


def test_unknown_topic_fails_loud(enabled_config):
    from nexus.mcp import core

    result = core.forensics("no-such-topic")
    assert "unknown" in result.lower()
    assert "chash-poison" in result  # names the known topics


def test_creds_present_runs_diagnostics_and_embeds_results(
    enabled_config, monkeypatch
):
    from nexus.db.diag_connection import DiagCredentials
    from nexus.mcp import core

    ran: list[tuple] = []

    def _resolve(creds_path=None):
        return DiagCredentials(port=5599, user="nexus_diag", password="x")

    def _run(statements, creds, **kw):
        ran.append(tuple(statements))
        return [str(i) for i in range(len(tuple(statements)))]

    monkeypatch.setattr(core, "_diag_resolve", _resolve)
    monkeypatch.setattr(core, "_diag_run", _run)

    result = core.forensics("chash-poison")
    assert ran, "diagnostics were not executed"
    # Every executed statement appears in the returned playbook alongside its
    # live result, so the agent sees actual store state, not placeholders.
    for stmt in ran[0]:
        assert stmt in result
    assert "= 0" in result or "-> 0" in result or ": 0" in result
    # The playbook carries the read-only framing and the runbook URL.
    assert "https://github.com/Hellblazer/nexus/blob/main/docs/migration-runbook.md" in result


def test_creds_absent_degrades_with_explicit_unavailable_note(
    enabled_config, monkeypatch
):
    from nexus.mcp import core

    monkeypatch.setattr(core, "_diag_resolve", lambda creds_path=None: None)
    called: list = []
    monkeypatch.setattr(
        core, "_diag_run", lambda *a, **k: called.append(a) or []
    )

    result = core.forensics("chash-poison")
    assert called == []  # nothing ran without credentials
    assert "unavailable" in result.lower()
    # NEVER a silent all-clean: the degrade note must not read as a result.
    assert "= 0" not in result
    assert "[chash-poison]" in result  # still a full playbook emission


def test_diag_failure_degrades_with_error_note(enabled_config, monkeypatch):
    from nexus.db.diag_connection import DiagCredentials
    from nexus.mcp import core

    monkeypatch.setattr(
        core, "_diag_resolve",
        lambda creds_path=None: DiagCredentials(port=5599, user="nexus_diag", password="x"),
    )

    def _boom(statements, creds, **kw):
        raise RuntimeError("psql exploded")

    monkeypatch.setattr(core, "_diag_run", _boom)
    result = core.forensics("chash-poison")
    assert "psql exploded" in result
    assert "[chash-poison]" in result  # playbook still emitted


def test_forensics_topic_sql_is_lint_clean():
    """The shipped forensics topic's diagnostic_sql must pass the P2.2 lint —
    its first live caller (Foundations-review note)."""
    from nexus.remediation import StoreState
    from nexus.remediation.playbook import emit_forensics_playbook

    pb = emit_forensics_playbook("chash-poison", StoreState(detail="x"))
    assert pb.diagnostic_sql, "forensics topic must carry diagnostic SQL"
    # emit_forensics_playbook already ran the lint (it raises on violation);
    # assert the statements are the aggregate shape — Amendment A6
    # (nexus-9bufb): per-table sums AGAINST THE COUNTS VIEW (structural
    # content boundary), plus the pg_constraint state read.
    for stmt in pb.diagnostic_sql:
        assert (
            stmt.upper().startswith(("SELECT SUM", "SELECT CONNAME"))
            or "diag_chash_conformance" in stmt
        ), stmt


def test_no_outbound_http_in_the_tool_path(enabled_config, monkeypatch):
    """The forensics path must never touch the network (RDR-182 A2: the
    product transmits no store content; psql to localhost only)."""
    import socket

    from nexus.mcp import core

    def _no_network(*a, **k):
        raise AssertionError("outbound network attempted from forensics()")

    monkeypatch.setattr(core, "_diag_resolve", lambda creds_path=None: None)
    monkeypatch.setattr(socket, "create_connection", _no_network)
    result = core.forensics("chash-poison")
    assert "[chash-poison]" in result
