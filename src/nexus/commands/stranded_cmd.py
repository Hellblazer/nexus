# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx stranded`` — the cloud-mode consented de-strand escape (nexus-cmtpa).

Why this exists: ``nexus.config._ladder_migration_verified`` (nexus-4922x)
proves a pre-PG-to-PG migration happened by reading the engine-side
upgrade-ladder completion record — sound in LOCAL mode (one bundled PG per
install), but that record is keyed ``(tenant_id, rung_name)`` only (see
``service/src/main/resources/db/changelog/ladder-001-baseline.xml``), so
in MANAGED/cloud mode a shared tenant across two machines would let one
machine's migration falsely de-strand another's own, distinct, unmigrated
data (bead nexus-cmtpa). ``nx stranded ack`` is the explicit, CONSENTED,
LOCAL, machine-scoped alternative for that case: it fingerprints the
pre-PG artifacts actually present on THIS machine (path + size + mtime,
see :func:`nexus.stranded_install.artifact_fingerprint`) and records a
local marker attesting the two-hop migration was completed for exactly
that artifact set. :func:`nexus.stranded_install.detect_stranded_install`
(via ``check_local_ack``, wired ONLY in cloud mode by
:func:`nexus.config.detect_stranded_install_default`) trusts the marker
only while the fingerprint still matches — if the artifact set changes
(new pre-PG data appears, or a different stranded box reuses this config
dir), the mismatch re-strands rather than trusting a stale attestation.

Command surface choice: a dedicated ``stranded`` group (not a ``nx
doctor`` flag) — ``nx doctor`` is a read-only diagnostic surface (health
checks only, no mutation), and this command WRITES a consent record, so
folding it into doctor would blur that boundary. Not ``nx upgrade``
either: upgrade is about the LADDER (a different, engine-verified
mechanism), and this command's whole reason to exist is that the ladder
signal is untrustworthy here — conflating the two verbs would suggest a
relationship that does not hold.

Consent semantics (deliberately explicit, mirroring
``stranded_install``'s own deletion policy — see its module docstring):
this command NEVER deletes anything. Deletion of pre-PG artifacts remains
a separate, third, independently consented act (unchanged, out of scope
here) — acking a migration is not a request to remove the rollback
copies.

BENIGN MTIME DRIFT: the fingerprint (path + size + mtime, see
:func:`nexus.stranded_install.artifact_fingerprint`) can legitimately
change WITHOUT any new pre-PG data appearing — a backup restore, a volume
remount, or a copy tool that does not preserve timestamps all touch
``mtime`` on files whose bytes are otherwise identical. That reads
exactly like drift to the fingerprint and re-strands. This is not a bug
to work around: re-verify the artifacts are still genuinely migrated (the
underlying data has not changed, only its timestamp), then simply
re-run ``nx stranded ack``.
"""
from __future__ import annotations

import click


@click.group("stranded")
def stranded_group() -> None:
    """Cloud-mode stranded-install de-strand tools (nexus-cmtpa)."""


@stranded_group.command("ack")
@click.option(
    "--yes", "-y", "assume_yes", is_flag=True, default=False,
    help="Skip the confirmation prompt.",
)
def ack_cmd(assume_yes: bool) -> None:
    """Attest that THIS machine's pre-PG data has already been migrated.

    Managed/cloud mode ONLY: the engine-side migration-completion signal
    is shared across every machine on the same tenant, so it cannot prove
    this specific machine's own pre-PG data reached PG (see this module's
    docstring). This command records a LOCAL marker, bound to a
    fingerprint of the pre-PG artifact files currently on this machine's
    disk, attesting:

        "I attest that I have completed the two-hop migration (installed
        the pinned release and ran `nx upgrade` there) for the pre-PG
        data currently on this machine."

    If those files later change (new pre-PG data appears under the same
    paths, or this config directory gets reused by a different stranded
    install), the fingerprint mismatches and the stranded-install refusal
    returns — the ack does not survive a changed artifact set.

    This command never deletes anything. It only records an attestation.
    """
    from nexus.config import _resolve_stranded_paths, is_local_mode  # noqa: PLC0415 — deferred import
    from nexus.stranded_install import (  # noqa: PLC0415 — deferred import
        ACK_CONSENT_TEXT,
        LAST_MIGRATION_CAPABLE,
        discover_artifacts,
        write_ack_marker,
    )

    if LAST_MIGRATION_CAPABLE is None:
        click.echo(
            "The stranded-install detector is disarmed on this release "
            "(it ships the migration tool) — there is nothing to ack."
        )
        return

    if is_local_mode():
        click.echo(
            "This install is in LOCAL mode: the engine-verified upgrade-"
            "ladder signal already governs de-stranding here (see `nx "
            "upgrade`), and is strictly stronger than a self-attestation. "
            "`nx stranded ack` has no effect in local mode — nothing was "
            "written."
        )
        return

    config_dir, chroma_dir, catalog_dir = _resolve_stranded_paths()
    artifacts = discover_artifacts(config_dir, chroma_dir, catalog_dir)
    if not artifacts:
        click.echo(
            "No pre-PG artifacts found on this machine — nothing to "
            "attest."
        )
        return

    click.echo("The following pre-PG artifact(s) were found on this machine:")
    for path in artifacts:
        click.echo(f"  {path}")
    click.echo("")
    click.echo(f"By continuing, you attest: {ACK_CONSENT_TEXT}")
    click.echo(
        "This does NOT delete these files — they remain on disk as "
        "copy-not-move rollback sources; deletion is a separate, "
        "explicitly consented step this command never performs."
    )
    click.echo(
        "If you have NOT completed the migration, do not proceed: "
        "acknowledging removes this warning permanently and any "
        "unmigrated data in these files will not be flagged again."
    )
    if not assume_yes and not click.confirm(
        "\nProceed and record this attestation?"
    ):
        click.echo("Aborted — nothing was written.")
        return

    record = write_ack_marker(config_dir, artifacts)
    click.echo("")
    click.echo(
        f"Recorded: fingerprint {record['fingerprint'][:12]}... at "
        f"{record['acked_at']} for {len(artifacts)} artifact(s)."
    )
    click.echo(
        "The stranded-install refusal will not trip again for this "
        "artifact set. If these files change, the fingerprint will no "
        "longer match and the refusal will return."
    )
