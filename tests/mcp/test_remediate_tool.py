# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-182 P3.2 (nexus-ykzbj.11): the ``remediate(topic, confirm)`` MCP tool.

The agent-invocable MUTATION-AUTHORIZING surface, with the explicit five-layer
contract IN ORDER — and the opt-in check NEVER collapsed into the confirm
flag:

  (1) opt-in gate  — flag false ⇒ exact refusal, zero work, REGARDLESS of
                     confirm (confirm=True with flag off must still refuse);
  (2) describe     — confirm=False ⇒ a description of what consent would
                     authorize: constraints + deliverable + runbook, but NOT
                     the ordered recovery steps (the mutation guidance is
                     withheld until consent), and NO consent row is written;
  (3) confirm=True — the explicit consent gesture;
  (4) mutate       — the consented release of the full recovery playbook
                     (the product hands GUIDANCE; the user's agent executes —
                     RDR-182 §5 trust boundary: the product itself never runs
                     the mutation);
  (5) audit-record — a consent row via Telemetry.record_consent, written
                     fail-closed: if the audit cannot be written (service
                     mode until nexus-ng2sy), the release is REFUSED — no
                     unaudited mutation authorization, ever.
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


@pytest.fixture()
def disabled_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg))
    monkeypatch.chdir(tmp_path)
    return cfg


class _ConsentRecorder:
    """Stands in for db.telemetry; records every consent write."""

    def __init__(self):
        self.rows: list[dict] = []

    def record_consent(self, *, scope: str, ts: str, granted: bool) -> None:
        self.rows.append({"scope": scope, "ts": ts, "granted": granted})


@pytest.fixture()
def consent_recorder(monkeypatch):
    from contextlib import contextmanager

    from nexus.mcp import core

    recorder = _ConsentRecorder()

    class _Db:
        telemetry = recorder

    @contextmanager
    def _ctx():
        yield _Db()

    monkeypatch.setattr(core, "_t2_ctx", _ctx)
    return recorder


@pytest.fixture()
def no_diag(monkeypatch):
    """Diagnostics degrade path (no live store state needed for these tests)."""
    from nexus.mcp import core

    monkeypatch.setattr(core, "_diag_resolve", lambda creds_path=None: None)
    return None


# ── Layer 1: the opt-in gate never collapses into confirm ───────────────────

def test_flag_off_refuses_regardless_of_confirm(disabled_config, consent_recorder, monkeypatch):
    """(critic-p3 High lock) Refusal does ZERO work — proven at the seams,
    same mechanical standard as forensics: neither diagnostic seam is
    touched and no consent row is written, for confirm=False AND True."""
    from nexus.mcp import core

    touched: list = []
    monkeypatch.setattr(
        core, "_diag_resolve",
        lambda creds_path=None: touched.append("resolve") or None,
    )
    monkeypatch.setattr(
        core, "_diag_run",
        lambda *a, **k: touched.append("run") or [],
    )
    assert core.remediate("chash-poison") == core._REMEDIATION_REFUSAL
    assert core.remediate("chash-poison", confirm=True) == core._REMEDIATION_REFUSAL
    assert touched == []  # zero diagnostic work on refusal
    assert consent_recorder.rows == []  # no consent row on ANY refused path


# ── Layer 2: describe (confirm=False) ────────────────────────────────────────

def test_describe_never_writes_consent_and_withholds_steps(
    enabled_config, consent_recorder, no_diag
):
    from nexus.mcp import core
    from nexus.remediation import StoreState, emit_playbook

    result = core.remediate("chash-poison")  # confirm defaults False
    assert consent_recorder.rows == []
    # Names the consent gesture:
    assert "confirm=true" in result.lower()
    # Carries the hard do-NOTs and the runbook pointer:
    assert "Do NOT drop the chash length constraints." in result
    assert "docs/migration-runbook.md" in result
    # WITHHOLDS the ordered recovery steps (the mutation guidance):
    steps = emit_playbook("chash-poison", StoreState(detail="x")).steps
    for step in steps:
        assert step not in result


# ── Layers 3+4+5: confirm releases the playbook and audits the consent ──────

def test_confirm_records_consent_and_releases_full_playbook(
    enabled_config, consent_recorder, no_diag
):
    from nexus.mcp import core
    from nexus.remediation import StoreState, emit_playbook

    result = core.remediate("chash-poison", confirm=True)
    assert len(consent_recorder.rows) == 1
    row = consent_recorder.rows[0]
    assert row["scope"] == "remediate:chash-poison"
    assert row["granted"] is True
    assert row["ts"]  # caller-supplied timestamp present
    # The full recovery playbook (with the ordered mutating steps) released:
    steps = emit_playbook("chash-poison", StoreState(detail="x")).steps
    for step in steps:
        assert step in result


def _refusal_for(monkeypatch, telemetry_obj):
    """Drive remediate(confirm=True) with a stubbed telemetry store."""
    from contextlib import contextmanager

    from nexus.mcp import core

    class _Db:
        telemetry = telemetry_obj

    @contextmanager
    def _ctx():
        yield _Db()

    monkeypatch.setattr(core, "_t2_ctx", _ctx)
    return core.remediate("chash-poison", confirm=True)


def _assert_playbook_withheld(result: str) -> None:
    from nexus.remediation import StoreState, emit_playbook

    for step in emit_playbook("chash-poison", StoreState(detail="x")).steps:
        assert step not in result, "playbook released despite a failed audit"


def test_release_is_refused_when_the_engine_lacks_the_consent_route(
    enabled_config, monkeypatch, no_diag
):
    """The REAL version-skew shape (nexus-huaef corrects this test's premise).

    This test used to stub a telemetry object with NO ``record_consent``
    attribute and call that "the real service-mode shape". It is not:
    HttpTelemetryStore has had ``record_consent`` since nexus-ng2sy, so the
    ``hasattr`` branch the test was pinning could never fire in production —
    the test made a dead branch look covered. Third instance today of a fixture
    encoding a handle shape that does not exist (cf. tests/test_catalog.py,
    tests/test_projection_quality.py).

    What an old engine ACTUALLY does: the method exists, posts, and the route
    404s. That must still produce the actionable "upgrade the engine" message,
    not a bare HTTP error.
    """
    import httpx

    class _OldEngineTelemetry:
        def record_consent(self, **_kw):
            request = httpx.Request("POST", "http://svc/v1/telemetry/consents/record")
            response = httpx.Response(404, request=request)
            raise httpx.HTTPStatusError("HTTP 404: not found", request=request,
                                        response=response)

    result = _refusal_for(monkeypatch, _OldEngineTelemetry())
    assert "nexus-ng2sy" in result, result
    assert "consent" in result.lower()
    _assert_playbook_withheld(result)


def test_release_is_refused_on_any_other_audit_failure(
    enabled_config, monkeypatch, no_diag
):
    """Fail-closed is UNCONDITIONAL — the skew diagnosis only changes the
    explanation, never whether the playbook is released.

    A 500 from a present-but-broken consents route is a real write failure and
    must NOT be mislabelled as a version skew (that would send the operator to
    upgrade an engine that is fine while their audit store is broken)."""
    import httpx

    class _BrokenStore:
        def record_consent(self, **_kw):
            request = httpx.Request("POST", "http://svc/v1/telemetry/consents/record")
            response = httpx.Response(500, request=request)
            raise httpx.HTTPStatusError("HTTP 500: boom", request=request,
                                        response=response)

    result = _refusal_for(monkeypatch, _BrokenStore())
    assert "REFUSING" in result
    assert "nexus-ng2sy" not in result, (
        "a 500 is a broken audit store, not a missing route — must not tell the "
        f"operator to upgrade the engine: {result}"
    )
    _assert_playbook_withheld(result)


def test_release_is_refused_when_the_store_raises_anything_at_all(
    enabled_config, monkeypatch, no_diag
):
    """Belt and braces on the contract itself: ANY exception withholds."""

    class _Exploding:
        def record_consent(self, **_kw):
            raise ValueError("unexpected")

    result = _refusal_for(monkeypatch, _Exploding())
    assert "REFUSING" in result
    _assert_playbook_withheld(result)


def test_release_refused_on_any_audit_write_failure(
    enabled_config, monkeypatch, no_diag
):
    """(critic-p3 High) Fail-closed auditing covers EVERY failure shape, not
    just the service-mode AttributeError: a locked SQLite / disk-full /
    migration bug produces the contract's refusal — never a raw traceback,
    never an unaudited release."""
    import sqlite3
    from contextlib import contextmanager

    from nexus.mcp import core
    from nexus.remediation import StoreState, emit_playbook

    class _FailingTelemetry:
        def record_consent(self, *, scope, ts, granted):
            raise sqlite3.OperationalError("database is locked")

    class _Db:
        telemetry = _FailingTelemetry()

    @contextmanager
    def _ctx():
        yield _Db()

    monkeypatch.setattr(core, "_t2_ctx", _ctx)
    result = core.remediate("chash-poison", confirm=True)
    assert "unaudited" in result.lower()
    assert "database is locked" in result
    steps = emit_playbook("chash-poison", StoreState(detail="x")).steps
    for step in steps:
        assert step not in result  # playbook NOT released


def test_remediate_only_topic_degrades_without_forensics_counterpart(
    enabled_config, consent_recorder, monkeypatch
):
    """(critic-p3 Low) A remediate topic with no forensics twin (the
    nexus-4s19o legacy-ids topic will be the first real one) takes the
    'no live diagnostics defined' path — and never touches the diag seams."""
    from nexus.mcp import core
    from nexus.remediation import playbook as pb_mod

    touched: list = []
    monkeypatch.setattr(
        core, "_diag_resolve",
        lambda creds_path=None: touched.append("resolve") or None,
    )
    monkeypatch.setitem(
        pb_mod._TOPICS, "remediate-only", pb_mod._chash_poison
    )
    result = core.remediate("remediate-only")
    assert "no live diagnostics defined" in result
    assert touched == []


# ── Misc contract edges ──────────────────────────────────────────────────────

def test_unknown_topic_fails_loud(enabled_config, consent_recorder, no_diag):
    from nexus.mcp import core

    result = core.remediate("no-such-topic", confirm=True)
    assert "unknown" in result.lower()
    assert "chash-poison" in result
    assert consent_recorder.rows == []  # no consent row for an unknown topic


def test_remediate_is_registered_at_the_mcp_boundary():
    from nexus.mcp.core import mcp

    tool_names = {t.name for t in mcp._tool_manager.list_tools()}
    assert "remediate" in tool_names


def test_live_diag_state_embedded_when_available(enabled_config, consent_recorder, monkeypatch):
    from nexus.db.diag_connection import DiagCredentials
    from nexus.mcp import core

    monkeypatch.setattr(
        core, "_diag_resolve",
        lambda creds_path=None: DiagCredentials(port=5599, user="nexus_diag", password="x"),
    )
    monkeypatch.setattr(
        core, "_diag_run",
        lambda statements, creds, **kw: ["7"] * len(tuple(statements)),
    )
    result = core.remediate("chash-poison", confirm=True)
    assert "= 7" in result  # live forensics counts inform the recovery context
