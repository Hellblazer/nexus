# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-182 opt-in gate at the MCP tool boundary — now on the REAL tool.

Originally the A4 gating spike (nexus-ykzbj.1, throwaway ``rdr182_gate_spike``);
P3.1 (nexus-ykzbj.10) replaced the spike with the real ``forensics`` tool, and
these tests moved with the gate: same mechanical guarantees, real surface.

Guarantees locked here:
  (a) flag absent/false/garbage/flat-scalar -> the EXACT refusal string, and
      ZERO diagnostic work happens (no credential resolution, no SQL run —
      proven with recording monkeypatches);
  (b) the flag enables via the REAL write path the refusal names
      (``set_config_value`` stores the STRING "true");
  (c) the string "false" refuses (bool("false") is True — the inversion trap);
  (d) the full truthy-synonym set and non-bool/str scalars are pinned;
  (e) the gate reads config FRESH per invocation (long-lived MCP server);
  (f) no env-override path exists for the consent flag;
  (g) the gated tool is REGISTERED on the core FastMCP instance.
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def isolated_config(tmp_path, monkeypatch):
    """Fresh config universe: empty NEXUS_CONFIG_DIR, cwd without .nexus.yml."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg))
    monkeypatch.chdir(tmp_path)
    return cfg


@pytest.fixture()
def diag_recorder(monkeypatch):
    """Record every touch of the diagnostic path; refusals must record NONE."""
    from nexus.mcp import core

    calls: dict[str, list] = {"resolve": [], "run": []}

    def _resolve(creds_path=None):
        calls["resolve"].append(creds_path)
        return None  # degrade path by default; tests override when needed

    def _run(statements, creds, **kw):
        calls["run"].append(tuple(statements))
        return ["0"] * len(tuple(statements))

    monkeypatch.setattr(core, "_diag_resolve", _resolve)
    monkeypatch.setattr(core, "_diag_run", _run)
    return calls


def test_default_is_exactly_enabled_false():
    from nexus.config import _DEFAULTS

    assert _DEFAULTS["claude_assisted_remediation"] == {"enabled": False}


def test_flag_absent_returns_exact_refusal_and_zero_diagnostic_work(
    isolated_config, diag_recorder
):
    from nexus.mcp import core

    result = core.forensics("chash-poison")
    assert result == core._REMEDIATION_REFUSAL
    assert diag_recorder["resolve"] == []
    assert diag_recorder["run"] == []


def test_enable_via_the_exact_command_the_refusal_names(
    isolated_config, diag_recorder
):
    from nexus.config import set_config_value
    from nexus.mcp import core

    set_config_value("claude_assisted_remediation.enabled", "true")
    result = core.forensics("chash-poison")
    assert result != core._REMEDIATION_REFUSAL
    assert "[chash-poison]" in result  # the playbook rendering
    assert "nx config set claude_assisted_remediation.enabled true" \
        in core._REMEDIATION_REFUSAL


def test_string_false_refuses_not_truthy(isolated_config, diag_recorder):
    from nexus.config import set_config_value
    from nexus.mcp import core

    set_config_value("claude_assisted_remediation.enabled", "false")
    assert core.forensics("chash-poison") == core._REMEDIATION_REFUSAL
    assert diag_recorder["run"] == []


def test_flat_scalar_section_refuses_without_crashing(isolated_config, diag_recorder):
    from nexus.mcp import core

    for flat in ("claude_assisted_remediation: true\n",
                 "claude_assisted_remediation: 'true'\n",
                 "claude_assisted_remediation: false\n"):
        (isolated_config / "config.yml").write_text(flat)
        assert core.forensics("chash-poison") == core._REMEDIATION_REFUSAL
    assert diag_recorder["run"] == []


def test_declared_truthy_string_synonyms_all_enable(isolated_config, diag_recorder):
    from nexus.config import set_config_value
    from nexus.mcp import core

    for synonym in ("true", "TRUE", "True", "1", "yes", "YES", " true "):
        set_config_value("claude_assisted_remediation.enabled", synonym)
        assert core.forensics("chash-poison") != core._REMEDIATION_REFUSAL, synonym


def test_non_bool_non_str_scalars_refuse(isolated_config, diag_recorder):
    from nexus.mcp import core

    for raw in ("enabled: 1", "enabled: null", "enabled: [true]",
                "enabled: {on: true}", "enabled: 1.0"):
        (isolated_config / "config.yml").write_text(
            "claude_assisted_remediation:\n  " + raw + "\n"
        )
        assert core.forensics("chash-poison") == core._REMEDIATION_REFUSAL, raw
    assert diag_recorder["run"] == []


def test_repo_local_nexus_yml_cannot_enable(isolated_config, diag_recorder):
    """(critic-p3 CRITICAL lock) A repo-committed .nexus.yml arrives via
    `git pull` — it is NOT a human consent gesture. The gate reads the
    GLOBAL config.yml ONLY; a cwd .nexus.yml flipping the flag must refuse,
    with zero diagnostic work and (on remediate) zero consent rows."""
    from pathlib import Path

    from nexus.mcp import core

    # cwd is tmp_path (isolated_config chdir'd there): plant the attack file.
    Path(".nexus.yml").write_text(
        "claude_assisted_remediation:\n  enabled: true\n"
    )
    assert core.forensics("chash-poison") == core._REMEDIATION_REFUSAL
    assert core.remediate("chash-poison", confirm=True) == core._REMEDIATION_REFUSAL
    assert diag_recorder["resolve"] == []
    assert diag_recorder["run"] == []
    # And the global file still wins when the user genuinely opts in:
    (isolated_config / "config.yml").write_text(
        "claude_assisted_remediation:\n  enabled: true\n"
    )
    assert core.forensics("chash-poison") != core._REMEDIATION_REFUSAL


def test_flag_has_no_env_override_path():
    from nexus.config import _ENV_OVERRIDES

    assert not any(
        "claude_assisted_remediation" in str(entry) for entry in _ENV_OVERRIDES
    )


def test_forensics_is_registered_at_the_mcp_boundary():
    from nexus.mcp.core import mcp

    tool_names = {t.name for t in mcp._tool_manager.list_tools()}
    assert "forensics" in tool_names
    assert "rdr182_gate_spike" not in tool_names  # the spike is gone


def test_gate_reads_config_fresh_per_invocation(isolated_config, diag_recorder):
    from nexus.config import set_config_value
    from nexus.mcp import core

    assert core.forensics("chash-poison") == core._REMEDIATION_REFUSAL
    set_config_value("claude_assisted_remediation.enabled", "true")
    assert core.forensics("chash-poison") != core._REMEDIATION_REFUSAL
    set_config_value("claude_assisted_remediation.enabled", "false")
    assert core.forensics("chash-poison") == core._REMEDIATION_REFUSAL
