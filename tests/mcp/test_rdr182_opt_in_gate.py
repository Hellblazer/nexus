# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-182 A4 gating spike: MCP tool-boundary opt-in refusal (nexus-ykzbj.1).

Critical Assumption A4: opt-in is enforceable AT THE MCP TOOL BOUNDARY — a
config-flag read as the tool's FIRST statement that refuses (returning a
string naming the exact enable command) BEFORE any diagnostic content is
built or emitted. ``click.confirm`` does not transfer to MCP, and no other
``@mcp.tool()`` is config-gated, so this mechanism is net-new and load-bearing
for the RDR-182 Desktop surface (Gap 4).

The spike proves, mechanically:
  (a) flag absent (default-off)  -> the EXACT refusal string, and the content
      path was never entered (zero emission before the gate);
  (b) flag enabled via the REAL write path the refusal names
      (``nx config set claude_assisted_remediation.enabled true`` ->
      ``set_config_value``, which stores the STRING ``"true"``) -> the content
      path is reached — a bool-only gate would refuse forever and make the
      refusal's own remedy a lie;
  (c) the string ``"false"`` refuses — naive truthiness would invert it
      (``bool("false") is True``), silently enabling on an explicit disable;
  (d) a real YAML bool ``true`` enables;
  (e) the default lives in ``config._DEFAULTS`` as exactly
      ``{"enabled": False}`` (the ``attention_guided_v1`` template).

The spike tool ``rdr182_gate_spike`` is THROWAWAY: RDR-182 Phase 3 replaces it
with the real ``forensics`` / ``remediate`` tools reusing the same gate helper
(``_remediation_opt_in`` + ``_REMEDIATION_REFUSAL``), then deletes it and its
entry in ``test_core_registered_tools``.
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def isolated_config(tmp_path, monkeypatch):
    """Fresh config universe: empty NEXUS_CONFIG_DIR, cwd without .nexus.yml."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg))
    # load_config merges <cwd>/.nexus.yml — point cwd at a bare dir so the
    # repo's own .nexus.yml cannot leak into the merge.
    monkeypatch.chdir(tmp_path)
    return cfg


def test_default_is_exactly_enabled_false():
    """_DEFAULTS carries the flag, default-off, exact equality (locked)."""
    from nexus.config import _DEFAULTS

    assert _DEFAULTS["claude_assisted_remediation"] == {"enabled": False}


def test_flag_absent_returns_exact_refusal_and_zero_content(isolated_config):
    """(a) Default-off: EXACT refusal string; content path never entered."""
    from nexus.mcp import core

    core._SPIKE_CONTENT_CALLS.clear()
    result = core.rdr182_gate_spike()
    assert result == core._REMEDIATION_REFUSAL
    assert core._SPIKE_CONTENT_CALLS == []


def test_enable_via_the_exact_command_the_refusal_names(isolated_config):
    """(b) The refusal's remedy actually works: set_config_value stores the
    STRING "true" (the ``nx config set`` write path) and the gate honors it."""
    from nexus.config import set_config_value
    from nexus.mcp import core

    set_config_value("claude_assisted_remediation.enabled", "true")
    core._SPIKE_CONTENT_CALLS.clear()
    result = core.rdr182_gate_spike()
    assert result == core._SPIKE_CONTENT_MARKER
    assert core._SPIKE_CONTENT_CALLS == ["content-built"]
    # The refusal string must NAME this exact command — lock the coupling.
    assert "nx config set claude_assisted_remediation.enabled true" \
        in core._REMEDIATION_REFUSAL


def test_string_false_refuses_not_truthy(isolated_config):
    """(c) The inversion trap: the STRING "false" is non-empty (truthy) but
    MUST refuse — an explicit disable can never read as enabled."""
    from nexus.config import set_config_value
    from nexus.mcp import core

    set_config_value("claude_assisted_remediation.enabled", "false")
    core._SPIKE_CONTENT_CALLS.clear()
    result = core.rdr182_gate_spike()
    assert result == core._REMEDIATION_REFUSAL
    assert core._SPIKE_CONTENT_CALLS == []


def test_yaml_bool_true_enables(isolated_config):
    """(d) A hand-written YAML bool (``enabled: true``) also enables."""
    from nexus.mcp import core

    (isolated_config / "config.yml").write_text(
        "claude_assisted_remediation:\n  enabled: true\n"
    )
    core._SPIKE_CONTENT_CALLS.clear()
    result = core.rdr182_gate_spike()
    assert result == core._SPIKE_CONTENT_MARKER
    assert core._SPIKE_CONTENT_CALLS == ["content-built"]


def test_garbage_value_fails_closed(isolated_config):
    """Unparseable flag values refuse (fail-closed), never enable."""
    from nexus.config import set_config_value
    from nexus.mcp import core

    set_config_value("claude_assisted_remediation.enabled", "banana")
    core._SPIKE_CONTENT_CALLS.clear()
    assert core.rdr182_gate_spike() == core._REMEDIATION_REFUSAL
    assert core._SPIKE_CONTENT_CALLS == []


def test_flat_scalar_section_refuses_without_crashing(isolated_config):
    """(critic-spike High) A hand-edited FLAT scalar — ``claude_assisted_
    remediation: true`` instead of the nested shape — makes _deep_merge
    replace the default dict with the bare scalar. The gate must REFUSE
    (fail-closed), never crash: an AttributeError is not a refusal."""
    from nexus.mcp import core

    for flat in ("claude_assisted_remediation: true\n",
                 "claude_assisted_remediation: 'true'\n",
                 "claude_assisted_remediation: false\n"):
        (isolated_config / "config.yml").write_text(flat)
        core._SPIKE_CONTENT_CALLS.clear()
        assert core.rdr182_gate_spike() == core._REMEDIATION_REFUSAL
        assert core._SPIKE_CONTENT_CALLS == []


def test_declared_truthy_string_synonyms_all_enable(isolated_config):
    """(critic-spike Medium) The helper's documented synonym set — "true",
    "1", "yes", case-insensitive — is locked in full, so narrowing or
    widening it silently breaks a pin."""
    from nexus.config import set_config_value
    from nexus.mcp import core

    for synonym in ("true", "TRUE", "True", "1", "yes", "YES", " true "):
        set_config_value("claude_assisted_remediation.enabled", synonym)
        assert core.rdr182_gate_spike() == core._SPIKE_CONTENT_MARKER, synonym


def test_non_bool_non_str_scalars_refuse(isolated_config):
    """(critic-spike Medium) YAML int 1 (a plausible hand-edit — bare ``1``
    parses as int, and ``1 is True`` is False), null, lists, dicts: all
    refuse. Only bool True and the string synonyms enable."""
    from nexus.mcp import core

    for raw in ("enabled: 1\n", "enabled: null\n", "enabled: [true]\n",
                "enabled: {on: true}\n", "enabled: 1.0\n"):
        (isolated_config / "config.yml").write_text(
            "claude_assisted_remediation:\n  " + raw.replace("\n", "\n  ").rstrip() + "\n"
        )
        core._SPIKE_CONTENT_CALLS.clear()
        assert core.rdr182_gate_spike() == core._REMEDIATION_REFUSAL, raw
        assert core._SPIKE_CONTENT_CALLS == []


def test_flag_has_no_env_override_path():
    """(critic-spike Low-5 lock) _ENV_OVERRIDES is the explicit allowlist for
    NX_* env overrides; this consent flag must never appear there — an env
    var is not a human consent gesture. Locks the negative so a future
    'for consistency' addition fails here."""
    from nexus.config import _ENV_OVERRIDES

    assert not any(
        "claude_assisted_remediation" in str(entry) for entry in _ENV_OVERRIDES
    )


def test_spike_tool_is_registered_at_the_mcp_boundary():
    """The gate sits on a REAL registered @mcp.tool() — the autonomously
    agent-invocable surface A4 is about — not on a demoted plain function."""
    from nexus.mcp.core import mcp

    tool_names = {t.name for t in mcp._tool_manager.list_tools()}
    assert "rdr182_gate_spike" in tool_names


def test_gate_reads_config_fresh_per_invocation(isolated_config):
    """The MCP server is long-lived: enabling mid-session must take effect on
    the NEXT call (no import-time caching of the flag)."""
    from nexus.config import set_config_value
    from nexus.mcp import core

    core._SPIKE_CONTENT_CALLS.clear()
    assert core.rdr182_gate_spike() == core._REMEDIATION_REFUSAL
    set_config_value("claude_assisted_remediation.enabled", "true")
    assert core.rdr182_gate_spike() == core._SPIKE_CONTENT_MARKER
    set_config_value("claude_assisted_remediation.enabled", "false")
    assert core.rdr182_gate_spike() == core._REMEDIATION_REFUSAL
    assert core._SPIKE_CONTENT_CALLS == ["content-built"]
