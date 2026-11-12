# SPDX-License-Identifier: AGPL-3.0-or-later
"""The CLI's side of the legacy-chash guard (nexus-f3yqd): what ``nx daemon service start`` does before and while it
waits for the supervisor, kept out of ``commands/daemon.py`` (a file at its size ceiling).

* :func:`clear_legacy_chash_refusal` runs first on every start: it keeps a pending refusal satisfiable (the marker
  schema in place, a record about another cluster retired) when Postgres can be asked, then looks for a backup that
  clears it, or raises the refusal (exit 75).
* :func:`legacy_chash_refusal` is the poll inside the lease waits: a pending refusal as the error to raise, and a
  supervisor that is waiting because it could not ask Postgres, said once on stderr.
* :func:`waiting_note` is what the wait's timeout error adds when the supervisor was waiting.
"""

from __future__ import annotations

from pathlib import Path

import click
import structlog

_log = structlog.get_logger(__name__)

_said: dict[str, str] = {}


def _refusal_error(config_dir: Path):
    from nexus.daemon import legacy_chash_guard as _guard  # noqa: PLC0415 — deferred import
    from nexus.daemon.storage_service_daemon import LegacyChashStartRefused  # noqa: PLC0415 — deferred import

    record = _guard.pending_refusal(config_dir)
    return LegacyChashStartRefused(_guard.refusal_text(record)) if record else None


def legacy_chash_refusal(config_dir: Path):
    """A pending refusal as a start error (exit 75), or None. While none is pending and the supervisor is waiting
    because it could not ask Postgres, its reason goes to stderr once per change."""
    refused = _refusal_error(config_dir)
    if refused is not None:
        return refused
    from nexus.daemon import legacy_chash_guard as _guard  # noqa: PLC0415 — deferred import

    text = _guard.wait_status_text(config_dir)
    if text is not None and _said.get(str(config_dir)) != text:
        _said[str(config_dir)] = text
        click.echo(text, err=True)
    return None


def waiting_note(config_dir: Path) -> str:
    """The reason the supervisor is waiting, as a line to add to a timeout error; empty when it is not."""
    from nexus.daemon import legacy_chash_guard as _guard  # noqa: PLC0415 — deferred import

    text = _guard.wait_status_text(config_dir)
    return f"\n{text}" if text else ""


def _reconcile(config_dir: Path) -> None:
    """Best effort: ask the running cluster, which the refusing supervisor left up, to keep the refusal true."""
    from nexus.daemon import legacy_chash_guard as _guard  # noqa: PLC0415 — deferred import
    from nexus.daemon.storage_service_daemon import _read_pg_credentials  # noqa: PLC0415 — deferred import
    from nexus.db.pg_provision import CREDENTIALS_FILENAME  # noqa: PLC0415 — deferred import
    from nexus.redact import redact_credentials  # noqa: PLC0415 — deferred import

    try:
        creds = _read_pg_credentials(config_dir / CREDENTIALS_FILENAME)
        outcome = _guard.reconcile_pending(
            config_dir, _guard.bundled_run_sql(config_dir, creds), creds.get("PG_DATA", "").strip(), recheck=True,
        )
    except Exception as exc:  # noqa: BLE001 — Postgres may be down; the record stands as it is
        _log.info("legacy_chash_guard_cli_could_not_reconcile", error=redact_credentials(str(exc)))
        return
    if outcome == "retired":
        click.echo("Legacy chash rows: the earlier refusal no longer applies to this database; starting.", err=True)


def clear_legacy_chash_refusal(config_dir: Path, legacy_backup: Path | None) -> None:
    """The user's next start clears a pending legacy-chash refusal with a verified, complete backup of this cluster, or
    is refused again with exit 75 (wren: explicit, never a file merely appearing)."""
    from nexus.daemon import legacy_chash_guard as _guard  # noqa: PLC0415 — deferred import

    if _guard.pending_refusal(config_dir) is None:
        return
    _reconcile(config_dir)
    if _guard.pending_refusal(config_dir) is None:
        return
    click.echo("Checking your backup (reading each candidate dump to its end)...", err=True)
    try:
        accepted = _guard.try_clear(config_dir, pg_restore=_guard.pg_restore_path(config_dir), explicit=legacy_backup)
    except _guard.PgRestoreUnavailable as exc:
        refused = _refusal_error(config_dir)
        assert refused is not None  # try_clear only runs on a pending refusal
        refused.args = (f"Cannot check your backup: pg_restore could not be run ({exc}). That is not a problem with "
                        f"the backup; fix the Postgres bundle (`nx doctor`), then start again.\n{refused.args[0]}",)
        raise refused from exc
    if accepted is not None:
        click.echo(f"Legacy chash rows: the backup {accepted} is a complete dump of this store; starting the engine, "
                   "which deletes the legacy rows at its first start.", err=True)
        return
    refused = _refusal_error(config_dir)
    if refused is not None:
        if legacy_backup is not None:
            refused.args = (f"{refused.args[0]}\nThe backup you named, {legacy_backup}, was not accepted: it must be "
                            "newer than the refusal, `pg_restore -l` must list the marker schema the refusal created "
                            "and the nexus chunk and manifest table data, and `pg_restore` must read all of it.",)
        raise refused
