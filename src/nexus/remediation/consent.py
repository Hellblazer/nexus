# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-182 durable opt-in reader — the ONE source of truth for the consent
flag, shared by the MCP tools (``nexus.mcp.core``) and the CLI commands
(``nexus.commands.remediation_cmd``) so the two surfaces cannot drift.
"""
from __future__ import annotations

import structlog

_log = structlog.get_logger(__name__)

__all__ = ["remediation_opt_in"]


def remediation_opt_in() -> bool:
    """True iff ``claude_assisted_remediation.enabled`` is affirmatively set
    in the user's GLOBAL ``~/.config/nexus/config.yml`` — and ONLY there.

    GLOBAL-ONLY, deliberately NOT ``load_config()`` (critic-p3 Critical,
    2026-07-12): the merged view honors a repo-local ``.nexus.yml``, and a
    consent flag that a ``git pull`` (or one approved PR) can flip is not a
    human consent gesture — an agent whose cwd is that checkout would clear
    the gate, receive the mutation-authorizing playbook, AND write a FALSE
    ``granted=True`` consent-audit row attributed to a user who never acted.
    The named enable command (``nx config set``) writes exactly the global
    file, so the remedy and the recognition surface coincide. Env vars are
    likewise not consulted.

    Read FRESH per invocation (the MCP server is long-lived; an operator
    enabling the flag mid-session must take effect on the next call).

    STRICT parse, fail-closed: ``nx config set`` stores the raw STRING
    (``set_config_value(dotted_key, value: str)``), so the flag arrives as
    ``"true"``/``"false"``, not a bool — and ``bool("false") is True``, so
    truthiness would invert an explicit disable. Only ``True`` and the
    case-insensitive strings ``"true"``/``"1"``/``"yes"`` enable; everything
    else (absent, ``False``, ``"false"``, non-bool/str scalars like YAML
    int ``1``, garbage) refuses. Fail-closed includes SHAPE mismatches
    (critic-spike High): a hand-edited flat scalar
    (``claude_assisted_remediation: true``) must refuse, not crash — a crash
    is not a refusal. An unreadable/corrupt global file also refuses.
    (The refusal's named command still crashes on a pre-existing flat scalar
    — set_config_value bug, tracked as nexus-s4a98.)

    Gates:
    - the MCP ``forensics``/``remediate`` tools ENTIRELY;
    - the CLI's live-credentialed diagnostics leg (RDR-182 taxonomy amendment
      A5, critic-p4 Critical) — the CLI's static playbook TEXT stays ungated
      (a human typing the command is the consent act), but the
      product-provisioned BYPASSRLS store probe honors the flag on every
      autonomously-reachable surface, not just the MCP transport;
    - the CLI ``nx remediate`` RELEASE (critic-final H1, 2026-07-13) — the
      guided-recovery handoff + its ``granted=True`` audit row require the
      flag in addition to the interactive confirm, so an automation piping
      ``y`` cannot forge a human-looking consent row. The describe stage
      stays ungated display.
    """
    import yaml  # noqa: PLC0415 — deferred, startup cost

    from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred, startup cost

    path = nexus_config_dir() / "config.yml"
    try:
        if not path.exists():
            return False
        data = yaml.safe_load(path.read_text()) or {}
    except Exception:  # noqa: BLE001 — unreadable/corrupt config = fail closed, never crash the gate
        return False
    if not isinstance(data, dict):
        return False
    section = data.get("claude_assisted_remediation", {})
    if not isinstance(section, dict):
        return False
    value = section.get("enabled", False)
    if value is True:
        return True
    return isinstance(value, str) and value.strip().lower() in ("true", "1", "yes")
