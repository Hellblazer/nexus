# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx forensics`` / ``nx remediate`` — RDR-182 P4 CLI surface.

Consent taxonomy (RDR-182 Gap 3, as amended 2026-07-13 / critic-p4 Critical):

- The playbook TEXT is DISPLAY-ONLY and UNGATED on both commands: a human
  typing the command and choosing what to copy IS the consent act (the
  shipped install-binary paste-to-Claude precedent). The static guidance is
  public runbook content.
- The LIVE-DIAGNOSTICS leg is GATED by ``claude_assisted_remediation.enabled``
  even on the CLI. The original taxonomy ("printing text is consent") was
  written for a static prompt with no DB access; ``forensics``/``remediate``
  additionally run a product-provisioned, credentialed BYPASSRLS store probe
  (``nexus.db.diag_connection.run_diagnostic_sql``) — the exact new capability
  Gap 3 exists to gate for autonomously-reachable surfaces. A Bash-capable
  agent shelling out to the CLI is as autonomous as one calling the MCP tool,
  so the live probe honors the flag on BOTH transports; flag-off degrades to
  the same "opt-in to include live counts" note (the guidance text is
  unaffected).
- ``nx remediate`` gates the RELEASE of the recovery playbook behind BOTH
  the durable opt-in flag AND a PER-INVOCATION ``click.confirm`` (defaults
  NO, aborts on EOF). The durable-flag requirement on the release (not just
  the confirm gesture — critic-final H1) means an automation piping ``y``
  cannot forge a human-looking ``granted=True`` audit row without the flag
  being set first. An accepted release is audit-recorded via
  ``Telemetry.record_consent`` FAIL-CLOSED before the playbook prints. The
  describe stage itself stays ungated display.

The playbook emitter (P1.3), the live-diagnostics choke point (P2.1/P3), and
the durable-flag reader (``nexus.remediation.consent.remediation_opt_in``,
shared with the MCP tools) are all reused — the CLI and MCP surfaces cannot
drift.
"""
from __future__ import annotations

import click

# Seam for tests: the live-diagnostics leg (patched to avoid a real PG).
def _live_detail(diagnostic_sql) -> str:  # noqa: ANN001, ANN202 — thin seam
    from nexus.db.diag_connection import live_store_detail  # noqa: PLC0415 — deferred, CLI startup cost

    return live_store_detail(diagnostic_sql)


#: The flag-off note for the credentialed live probe (RDR-182 taxonomy
#: amendment): the guidance text prints regardless; only the live store
#: counts are opt-in.
_LIVE_DIAG_OPT_IN_NOTE = (
    "live store counts NOT included — the credentialed diagnostic probe is "
    "opt-in (it runs a product-provisioned read against your store). Enable "
    "with `nx config set claude_assisted_remediation.enabled true` to embed "
    "live counts; the recovery guidance above is unaffected."
)


def _detail_for(topic: str) -> str:
    """Live store state when the topic has a forensics twin AND the durable
    opt-in is set; an honest opt-in / no-diagnostics note otherwise.

    The credentialed BYPASSRLS probe is gated even on the CLI (critic-p4
    Critical): the static playbook text is ungated display, but the live
    store read is the new capability Gap 3 gates on every
    autonomously-reachable surface.
    """
    from nexus.remediation import (  # noqa: PLC0415 — deferred, CLI startup cost
        StoreState,
        emit_forensics_playbook,
        forensics_topics,
    )
    from nexus.remediation.consent import remediation_opt_in  # noqa: PLC0415 — deferred, CLI startup cost

    if topic not in forensics_topics():
        return "no live diagnostics defined for this topic"
    if not remediation_opt_in():
        return _LIVE_DIAG_OPT_IN_NOTE
    probe = emit_forensics_playbook(topic, StoreState(detail=""))
    return _live_detail(probe.diagnostic_sql)


@click.command("forensics")
@click.argument("topic", default="chash-poison")
def forensics_cmd(topic: str) -> None:
    """Print the read-only diagnostic playbook for TOPIC (display-only).

    Ungated by design: printing guidance a human chose to request is the
    consent act. Runs the topic's lint-verified read-only diagnostics via the
    nexus_diag path when available and embeds live store state.
    """
    from nexus.remediation import (  # noqa: PLC0415 — deferred, CLI startup cost
        StoreState,
        emit_forensics_playbook,
        forensics_topics,
    )

    if topic not in forensics_topics():
        raise click.ClickException(
            f"unknown forensics topic {topic!r} — known topics: "
            f"{list(forensics_topics())}"
        )
    detail = _detail_for(topic)
    click.echo(emit_forensics_playbook(topic, StoreState(detail=detail)).tool_return())


@click.command("remediate")
@click.argument("topic", default="chash-poison")
@click.option(
    "--history", "show_history", is_flag=True, default=False,
    help="Print the consent-audit trail (grants and revokes, in order) "
         "and exit — the read surface for the "
         "claude_assisted_remediation_consents table.",
)
def remediate_cmd(topic: str, show_history: bool) -> None:
    """Guided recovery for TOPIC: describe, confirm per-invocation, then
    release the playbook with the consent audit-recorded.

    The confirm defaults to NO and aborts on EOF, so there is no blind or
    default consent and no --yes flag to fat-finger; a caller must supply an
    affirmative answer (an automation piping ``y`` is making that answer
    explicitly). Declining is safe — nothing runs, nothing is recorded, and
    the runbook URL stays on screen.
    """
    from datetime import datetime, timezone  # noqa: PLC0415 — deferred, CLI startup cost

    from nexus.commands._helpers import t2_handle  # noqa: PLC0415 — deferred, CLI startup cost
    from nexus.remediation import (  # noqa: PLC0415 — deferred, CLI startup cost
        StoreState,
        consent_scope,
        emit_playbook,
        remediate_topics,
    )

    if show_history:
        # The consent-audit READ surface (nexus-ykzbj.15): the trail is
        # inspectable by the operator, not just written. Read-only — safe
        # in any mode where the store answers; the service-mode parity gap
        # surfaces the same ng2sy message as the write side.
        with t2_handle() as db:
            if not hasattr(db.telemetry, "list_consents"):
                raise click.ClickException(
                    "Consent-audit history is unavailable in this deployment "
                    "(service-mode T2 lacks the consent surface until "
                    "nexus-ng2sy)."
                )
            rows = db.telemetry.list_consents()
        if not rows:
            click.echo("No consent events recorded.")
            return
        for row in rows:
            verb = "GRANT " if row["granted"] else "REVOKE"
            click.echo(f"{row['ts']}  {verb}  {row['scope']}")
        return

    if topic not in remediate_topics():
        raise click.ClickException(
            f"unknown remediate topic {topic!r} — known topics: "
            f"{list(remediate_topics())}"
        )
    playbook = emit_playbook(topic, StoreState(detail=_detail_for(topic)))
    # Pre-consent description: hard do-NOTs front-and-center + the clickable
    # runbook URL, which REMAINS on screen whatever the answer below. This
    # DISPLAY stays ungated (public runbook guidance).
    click.echo(playbook.describe(
        consent_hint="opt in, then answer the prompt (the grant is audit-recorded)"
    ))

    # RELEASE GATE (critic-final H1, Hal decision 2026-07-13): the guided
    # release + its granted=True audit row now ALSO require the durable
    # opt-in flag — not just the interactive confirm — so an automation
    # piping `y` cannot forge a human-looking consent row. The describe above
    # is ungated display; the mutation-authorizing handoff needs the same
    # durable opt-in the MCP tool needs. Shared reader (no drift).
    from nexus.remediation.consent import remediation_opt_in  # noqa: PLC0415 — deferred, CLI startup cost

    if not remediation_opt_in():
        click.echo("")
        raise click.ClickException(
            "The guided recovery release is opt-in. Enable it with "
            "`nx config set claude_assisted_remediation.enabled true`, then "
            "re-run and confirm. (The guidance above is display-only and "
            "needs no opt-in; only the audited playbook handoff does.)"
        )

    click.echo("")
    if not click.confirm(
        "Consent to receive the guided recovery playbook "
        "(recorded to the audit trail)?",
        default=False,
    ):
        click.echo(
            "Declined — nothing released, no consent recorded. The runbook "
            "URL above remains available."
        )
        return

    # FAIL-CLOSED audit before release (same contract as the MCP tool).
    try:
        with t2_handle() as db:
            if not hasattr(db.telemetry, "record_consent"):
                raise click.ClickException(
                    "Cannot record the consent audit in this deployment "
                    "(service-mode T2 lacks record_consent until nexus-ng2sy) "
                    "— refusing to release the recovery playbook unaudited."
                )
            db.telemetry.record_consent(
                scope=consent_scope("remediate", topic),
                ts=datetime.now(timezone.utc).isoformat(),
                granted=True,
            )
    except click.ClickException:
        raise
    except Exception as exc:  # noqa: BLE001 — fail-closed auditing: no unaudited release
        raise click.ClickException(
            f"Consent audit write FAILED ({exc}) — refusing to release the "
            "recovery playbook unaudited. Fix the audit store and re-run."
        ) from exc

    click.echo("")
    click.echo(playbook.tool_return())