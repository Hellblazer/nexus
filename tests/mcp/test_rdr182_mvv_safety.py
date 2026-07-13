# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-182 P5.1 (nexus-ykzbj.17): MVV proof (a) — the safety property.

THE durable regression lock for the autonomous-enlistment safety property:
with ``claude_assisted_remediation.enabled`` false (the default), the
credentialed-diagnostic capability is closed on BOTH autonomously-reachable
surfaces (taxonomy amendment A5):

- MCP ``forensics`` / ``remediate`` return the exact refusal string and emit
  ZERO diagnostic content (no runbook URL, no SQL, no live counts) — for
  every ``confirm`` value.
- CLI ``nx forensics`` / ``nx remediate`` print only the ungated guidance
  TEXT and do NOT run the credentialed BYPASSRLS probe (the live-diagnostics
  seam is never entered).

Non-vacuous: a gate mutation that let either surface through when the flag
is false must fail this test. The recording seams (``_diag_resolve`` /
``_diag_run`` on the MCP side, ``_live_detail`` on the CLI side) make
"zero diagnostic work" mechanically observable, not inferred.
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from nexus.commands.remediation_cmd import forensics_cmd, remediate_cmd
from nexus.mcp import core

_URL = "https://github.com/Hellblazer/nexus/blob/main/docs/migration-runbook.md"
#: Diagnostic-content markers that MUST be absent from a refused MCP return.
_DIAG_MARKERS = (_URL, "SELECT count(", "chash", "live diagnostic")


@pytest.fixture()
def flag_off(tmp_path, monkeypatch):
    """Default posture: fresh global config dir, flag never set, cwd clean."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg))
    monkeypatch.chdir(tmp_path)
    return cfg


@pytest.fixture()
def mcp_diag_spy(monkeypatch):
    calls: list = []
    monkeypatch.setattr(core, "_diag_resolve",
                        lambda creds_path=None: calls.append("resolve") or None)
    monkeypatch.setattr(core, "_diag_run",
                        lambda *a, **k: calls.append("run") or [])
    return calls


# ── MCP surface: refusal + zero content, every confirm value ────────────────

def test_mcp_forensics_refuses_with_zero_content(flag_off, mcp_diag_spy):
    out = core.forensics("chash-poison")
    assert out == core._REMEDIATION_REFUSAL
    assert mcp_diag_spy == []
    for marker in _DIAG_MARKERS:
        assert marker not in out


@pytest.mark.parametrize("confirm", [False, True])
def test_mcp_remediate_refuses_with_zero_content(flag_off, mcp_diag_spy, confirm):
    out = core.remediate("chash-poison", confirm=confirm)
    assert out == core._REMEDIATION_REFUSAL
    assert mcp_diag_spy == []
    for marker in _DIAG_MARKERS:
        assert marker not in out


# ── CLI surface: guidance text ungated, credentialed probe NOT run ──────────

@pytest.fixture()
def cli_probe_spy(monkeypatch):
    import nexus.commands.remediation_cmd as rc

    calls: list = []
    monkeypatch.setattr(rc, "_live_detail",
                        lambda sql: calls.append("probe") or "LIVE COUNTS")
    return calls


def test_cli_forensics_runs_no_credentialed_probe_when_flag_off(flag_off, cli_probe_spy):
    result = CliRunner().invoke(forensics_cmd, ["chash-poison"])
    assert result.exit_code == 0, result.output
    assert cli_probe_spy == []            # the BYPASSRLS probe never ran
    assert "LIVE COUNTS" not in result.output
    assert "opt-in" in result.output      # explicit, not silent
    assert "[chash-poison]" in result.output  # ungated guidance still prints


def test_cli_remediate_runs_no_credentialed_probe_when_flag_off(flag_off, cli_probe_spy):
    # EOF stdin: describe stage prints, confirm aborts — either way the probe
    # must not have run.
    result = CliRunner().invoke(remediate_cmd, ["chash-poison"])
    assert cli_probe_spy == []
    assert "LIVE COUNTS" not in result.output


# ── Non-vacuity: the property is enforced by the gate, not the fixtures ─────

def test_property_would_break_if_the_shared_gate_allowed_through(flag_off, monkeypatch, mcp_diag_spy):
    """Mutate the ONE shared reader to allow-through and confirm the safety
    property collapses — proves the test is coupled to the real gate, not a
    tautology over the fixtures."""
    import nexus.remediation.consent as consent_mod

    monkeypatch.setattr(consent_mod, "remediation_opt_in", lambda: True)
    out = core.forensics("chash-poison")
    assert out != core._REMEDIATION_REFUSAL          # gate opened -> not refused
    assert mcp_diag_spy != []                          # diagnostic work now happens
