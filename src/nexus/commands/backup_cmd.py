# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx backup``: an explicit, user-run dump of the local engine's database (nexus-f3yqd.4).

A verb the user runs, nothing else: no other command, hook or daemon calls it. The work is in
:mod:`nexus.backup.pg_backup`; ``tests/test_nx_backup_pin.py`` fails on a Python import or a command line that
runs it from anywhere but here (see that module's docstring for what the scan can and cannot see).
"""

from __future__ import annotations

import contextlib
import os
import signal
from collections.abc import Iterator
from pathlib import Path

import click

from nexus.backup.pg_backup import (
    ONLY_COPY_SENTENCE,
    BackupFailed,
    BackupRefused,
    already_there,
    database_size,
    default_destination,
    free_space_warning,
    resolve_target,
    restore_sections,
    run_dump,
)
from nexus.progress.step_report import StepReporter, format_bytes, format_duration

#: Where the full restore runbook lives: a path in the repository, and its address on GitHub (which
#: answers 404 until the release that carries the page reaches main).
RUNBOOK_URL = "https://github.com/Hellblazer/nexus/blob/main/docs/operations/backup-restore.md"

#: Signals that end the verb the way Ctrl-C does: pg_dump is stopped and the partial file removed.
_INTERRUPT_SIGNALS = tuple(
    getattr(signal, name) for name in ("SIGTERM", "SIGHUP") if hasattr(signal, name)
)


@contextlib.contextmanager
def _signals_as_interrupt() -> Iterator[None]:
    """SIGTERM (a kill) and SIGHUP (a closed terminal) clean up like Ctrl-C, instead of the process dying
    with pg_dump orphaned and its dump half written. The previous handlers come back afterwards. Only the
    main thread can install handlers; anywhere else this does nothing and the default action applies."""

    def _raise(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    installed: list[tuple[int, object]] = []
    try:
        for sig in _INTERRUPT_SIGNALS:
            previous = signal.signal(sig, _raise)
            # None means a handler installed from C: it cannot be put back, the default is the nearest thing.
            installed.append((sig, previous if previous is not None else signal.SIG_DFL))
    except ValueError:  # not the main thread
        pass
    try:
        yield
    finally:
        for sig, previous in installed:
            signal.signal(sig, previous)


@click.command("backup")
@click.option(
    "--to", "to", type=click.Path(dir_okay=False, path_type=Path), default=None,
    help="Where to write the dump (default: ~/nexus-backups/nexus-<UTC timestamp>.dump). An existing file is "
         "refused, never overwritten.",
)
def backup(to: Path | None) -> None:
    """Back up the local engine's database to one file, now.

    Runs `pg_dump -Fc` with the bundled Postgres against the running local cluster: an online,
    consistent snapshot of every tenant's data while the engine keeps serving, checked with
    `pg_restore -l` before it is kept. The dump is NOT compressed (the bundle is built without zlib):
    expect a file about the size of the database on disk. A failed, interrupted or killed run leaves
    no partial file.

    There is no automatic backup; this file is the only copy besides the live database. Copy it to
    another disk or machine.

    Local mode only: cloud backups are on the conexus side. There is no restore command; the printed
    commands and the backup-and-restore runbook (the URL it prints) restore into a fresh database and
    overwrite nothing until you swap names.
    """
    try:
        target = resolve_target()
        dest = (to if to is not None else default_destination()).expanduser().absolute()
        try:
            exists = os.path.lexists(dest)
        except ValueError as exc:  # a NUL byte in the path
            raise BackupRefused(f"cannot write the backup at {dest}: {exc}") from None
        if exists:
            raise already_there(dest)
    except BackupRefused as exc:
        raise click.ClickException(str(exc)) from None

    estimate = database_size(target)
    click.echo(f"Backing up the local database to {dest}")
    if estimate:
        click.echo(
            f"The database is about {format_bytes(estimate)} on disk (indexes are not dumped, so the file "
            "can be smaller or larger)."
        )
    warning = free_space_warning(dest, estimate)
    if warning:
        click.echo(f"Warning: {warning}", err=True)
    reporter = StepReporter(1, name="nx backup")
    try:
        with (
            _signals_as_interrupt(),
            reporter.step("Write the backup", "pg_dump -Fc, online, not compressed", lazy=True) as step,
        ):

            def progress(done: int) -> None:
                rate = done / max(step.elapsed(), 1e-3)
                step.progress(done, None, "bytes", note=f"written, {format_bytes(rate)}/s")

            try:
                result = run_dump(target, dest, on_progress=progress)
            except BackupRefused as exc:
                raise click.ClickException(str(exc)) from None
    except BackupFailed as exc:
        # the reporter already printed "failed after ...: <first line>"; the rest of pg_dump's words follow
        rest = exc.detail.splitlines()[1:]
        if rest:
            click.echo("\n".join(f"  {line}" for line in rest), err=True)
        raise click.exceptions.Exit(1) from None

    click.echo(f"Backup written: {result.path}")
    click.echo(f"Size: {format_bytes(result.size)}, in {format_duration(result.seconds)}")
    click.echo("")
    click.echo(ONLY_COPY_SENTENCE)
    click.echo("")
    click.echo(
        "To restore it into a FRESH database (this overwrites nothing until you swap names), after "
        "stopping the engine:"
    )
    sections = restore_sections(str(target.bin_dir), target.port, target.user, str(result.path))
    for line in sections["restore"].splitlines():
        click.echo(f"  {line}")
    if target.password:
        from nexus import config as _config  # noqa: PLC0415 - only the epilogue needs it

        click.echo(f"Set PGPASSWORD to PG_SUPERUSER_PASS from {_config.nexus_config_dir() / 'pg_credentials'} first.")
    click.echo(f"The whole runbook, including the swap and the undo: {RUNBOOK_URL}")
