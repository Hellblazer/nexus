# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx backup``: an explicit, user-run backup (nexus-f3yqd.4, nexus-csayy.3) and ``nx backup restore`` (nexus-csayy.6).

A verb the user runs, nothing else: no other command, hook or daemon calls it. Local mode dumps the local engine's
database (:mod:`nexus.backup.pg_backup`); managed (cloud) mode pulls the caller's own tenant through the engine's
export session into one ``.nxtenant`` file (:mod:`nexus.backup.cloud_export`, RDR-248); ``--check FILE`` verifies such
a file offline (:mod:`nexus.backup.tenant_file`); ``restore FILE`` loads such a file into the caller's own, empty
tenant through the engine's import session (:mod:`nexus.backup.cloud_import`) with a tenant or data token.
``tests/test_nx_backup_pin.py`` fails on a Python import or a command line that runs any of it from anywhere but
here (see that module's docstring for what the scan can and cannot see).
"""

from __future__ import annotations

import contextlib
import os
import signal
from collections.abc import Callable, Iterator
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
from nexus.backup import cloud_import
from nexus.backup.cloud_export import OMITTED_NOTICE, connect, default_file_name, run_export
from nexus.backup.tenant_file import TenantFileError, verify_file
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


@click.group("backup", invoke_without_command=True)
@click.option(
    "--to", "to", type=click.Path(dir_okay=False, path_type=Path), default=None,
    help="Where to write the backup (default: ~/nexus-backups/nexus-<UTC timestamp>.dump locally, "
         "~/nexus-backups/nexus-<tenant>-<UTC timestamp>.nxtenant in cloud mode). An existing file is "
         "refused, never overwritten.",
)
@click.option(
    "--check", "check", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None,
    help="Verify a cloud backup file (.nxtenant) offline, without contacting any engine, and print its header "
         "and tables. A damaged file is refused naming the table.",
)
@click.pass_context
def backup(ctx: click.Context, to: Path | None, check: Path | None) -> None:
    """Back up the local engine's database, or your cloud tenant, to one file, now.

    Local mode runs `pg_dump -Fc` with the bundled Postgres against the running local cluster: an online,
    consistent snapshot of every tenant's data while the engine keeps serving, checked with
    `pg_restore -l` before it is kept. The dump is NOT compressed (the bundle is built without zlib):
    expect a file about the size of the database. A failed, interrupted or killed run leaves
    no partial file.

    Cloud mode (managed service) asks the engine for an export of YOUR tenant, one consistent snapshot read page by
    page with your tenant or data token, and writes it to a `.nxtenant` file (owner-readable only), checked
    before it is kept. An engine older than the export routes is refused, naming the version it needs. The file
    omits operational state (tuples, in-flight jobs, session scratch, logs).

    `--check FILE` verifies a `.nxtenant` file offline: every page's hash, every table's count and checksum
    against the file's trailer.

    There is no automatic backup; this file is the only copy besides the live database. Copy it to
    another disk or machine.

    `nx backup restore FILE` loads a `.nxtenant` file into your own, empty tenant (see its help). A whole local
    database is restored with the printed commands and the backup-and-restore runbook (the URL it prints): they
    restore into a fresh database and overwrite nothing until you swap names.
    """
    if ctx.invoked_subcommand is not None:
        if to is not None or check is not None:
            raise click.UsageError("--to and --check belong to `nx backup` itself, not to a subcommand.")
        return
    if check is not None:
        if to is not None:
            raise click.UsageError("--check verifies a file that already exists and --to names a file to write; use one of them.")
        _check(check)
        return
    from nexus import config as _config  # noqa: PLC0415 - read through the module, so a test's mode patch is seen

    if not _config.is_local_mode():
        _cloud_backup(to)
        return
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


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _cloud_backup(to: Path | None) -> None:
    """Managed mode: export the caller's own tenant (RDR-248 "Client")."""
    try:
        client = connect()
        if to is not None:
            destination: Path | Callable[[str], Path] = to.expanduser().absolute()
            try:
                taken = os.path.lexists(destination)
            except ValueError as exc:  # a NUL byte in the path
                raise BackupRefused(f"cannot write the backup at {destination}: {exc}") from None
            if taken:
                raise already_there(destination)
        else:
            directory = default_destination().parent
            destination = lambda tenant: directory / default_file_name(tenant)  # noqa: E731 - the tenant is known only after the session opens
    except BackupRefused as exc:
        raise click.ClickException(str(exc)) from None

    click.echo("Backing up your cloud tenant from the managed service (one consistent snapshot, read page by page).")
    reporter = StepReporter(1, name="nx backup")
    try:
        with (
            _signals_as_interrupt(),
            reporter.step("Write the backup", "tenant export session", lazy=True) as step,
        ):

            def progress(done: int, table: str, index: int, total: int, rows: int) -> None:
                rate = done / max(step.elapsed(), 1e-3)
                step.progress(done, None, "bytes", note=f"table {index}/{total} {table}, {rows} rows, {format_bytes(rate)}/s")

            try:
                result = run_export(client, destination, on_progress=progress, on_note=lambda m: click.echo(m, err=True))
            except BackupRefused as exc:
                raise click.ClickException(str(exc)) from None
    except BackupFailed as exc:
        rest = exc.detail.splitlines()[1:]
        if rest:
            click.echo("\n".join(f"  {line}" for line in rest), err=True)
        raise click.exceptions.Exit(1) from None
    finally:
        client.close_http()

    h = result.header
    click.echo(f"Backup written: {result.path}")
    click.echo(f"Size: {format_bytes(result.size)}, in {format_duration(result.seconds)}")
    click.echo(f"Tenant {h['tenant_id']}, engine {h.get('engine_version', '?')}, schema {h['schema_changeset_count']} changesets.")
    for cls in ("data", "derived"):
        if cls in result.rows_by_class:
            n_tables = sum(1 for t in result.tables if t[1] == cls)
            click.echo(f"  {cls}: {_plural(result.rows_by_class[cls], 'row')} in {_plural(n_tables, 'table')}")
    if result.sensitive_documents:
        click.echo("")
        click.echo(
            f"This file holds {_plural(result.sensitive_documents, 'sensitive document')} (the ones you marked so an LLM never "
            "reads them). They are in the backup like everything else of yours; the file is readable by you only, so keep it that way."
        )
    click.echo("")
    click.echo(OMITTED_NOTICE)
    click.echo("")
    click.echo(ONLY_COPY_SENTENCE)
    click.echo("")
    click.echo(f"Verify it any time, offline: nx backup --check {result.path}")
    click.echo(
        f"Restore it into an empty tenant with: nx backup restore {result.path}   (the runbook: {RUNBOOK_URL})"
    )


def _check(path: Path) -> None:
    """``nx backup --check FILE``: offline, no engine, no mode."""
    try:
        report = verify_file(path)
    except TenantFileError as exc:
        text = str(exc)
        if "is not a nexus tenant backup" in text:
            text += " (a local `nx backup` dump is checked with the bundled `pg_restore -l`; see the runbook)"
        raise click.ClickException(text) from None
    h = report.header
    by_class = report.rows_by_class()
    total = sum(by_class.values())
    click.echo(f"{path}: OK (every page, table and the whole file verified; no engine was contacted)")
    click.echo(f"  Tenant: {h['tenant_id']}")
    click.echo(f"  Engine: {h.get('engine_version', '?')} (export format {h['format_version']}), schema {h['schema_changeset_count']} changesets")
    click.echo(f"  Snapshot taken: {h.get('created_at', '?')}")
    click.echo(f"  Size: {format_bytes(report.size)}")
    classes = ", ".join(f"{cls} {n}" for cls, n in sorted(by_class.items()))
    click.echo(f"  Tables: {len(report.tables)}, rows: {total} ({classes})")
    click.echo("")
    width = max(len(t.name) for t in report.tables)
    for t in report.tables:
        click.echo(f"  {t.name:<{width}}  {t.table_class:<8}  {_plural(t.rows, 'row'):<14}  {_plural(t.pages, 'page')}")


@backup.command("restore")
@click.argument("file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def restore(file: Path) -> None:
    """Load a `.nxtenant` backup into YOUR tenant, which must be empty.

    The file is verified end to end first (every page and table, as `--check` does); a damaged file is refused
    before anything is sent. The engine then stages the file in one transaction, in parts of at most 8 MiB, and
    loads it in one more: it either all lands or none of it does. Ids are assigned afresh, so the restored rows
    refer to each other and not to the old tenant's ids. Do not use the tenant until the verb reports the outcome:
    a write during the upload makes the engine refuse the load.

    Needs a tenant or data token for the empty tenant. On the managed service that is the `service_token` the operator
    issued. A local engine is the same routes, but this install's own token there is the supervisor's operator token,
    which the import refuses; issue a tenant token with `nx service token issue --tenant <tenant>` (`default` for
    the install's own tenant, which must be empty) and export it as NX_SERVICE_TOKEN (a local install reads the
    token from that variable). An engine older than the import routes is refused, naming the release
    that has them. If the connection drops while the load runs, the verb reads the engine's status, and after an
    engine restart its restore marker, and says whether the load committed; if it cannot learn that it says so and
    leaves the load alone.
    """
    try:
        client = cloud_import.connect()
    except BackupRefused as exc:
        raise click.ClickException(str(exc)) from None
    click.echo(f"Restoring {file} into your tenant on the service.")
    reporter = StepReporter(1, name="nx backup restore")
    try:
        with (
            _signals_as_interrupt(),
            reporter.step("Restore the backup", "tenant import session", lazy=True) as step,
        ):

            def progress(done: int, total: int, table: str, index: int, count: int) -> None:
                rate = done / max(step.elapsed(), 1e-3)
                step.progress(done, total or None, "bytes", note=f"table {index}/{count} {table}, {format_bytes(rate)}/s")

            try:
                result = cloud_import.run_restore(
                    client, file, on_progress=progress, on_note=lambda m: click.echo(m, err=True),
                )
            except BackupRefused as exc:
                raise click.ClickException(str(exc)) from None
    except BackupFailed as exc:
        rest = exc.detail.splitlines()[1:]
        if rest:
            click.echo("\n".join(f"  {line}" for line in rest), err=True)
        raise click.exceptions.Exit(1) from None
    finally:
        client.close_http()
    _print_restore(file, result)


def _print_restore(file: Path, result: cloud_import.RestoreResult) -> None:
    """What the committed load did, from the engine's report (or, after a lost session, its marker)."""
    out = result.outcome
    h = result.header
    click.echo(f"Restore committed: {file}")
    click.echo(
        f"From tenant {h['tenant_id']}, engine {h.get('engine_version', '?')}, schema {h['schema_changeset_count']} "
        f"changesets; took {format_duration(result.seconds)}."
    )
    report = out.report
    tables = report.get("tables") if isinstance(report.get("tables"), list) else []
    if tables:
        loaded: dict[str, int] = {}
        counted: dict[str, int] = {}
        for t in tables:
            cls = result.classes.get(str(t.get("table")), "data")
            loaded[cls] = loaded.get(cls, 0) + int(t.get("inserted") or 0)
            counted[cls] = counted.get(cls, 0) + 1
        for cls in ("data", "derived"):
            if cls in loaded:
                click.echo(f"  {cls}: {_plural(loaded[cls], 'row')} loaded in {_plural(counted[cls], 'table')}")
        seeded = sum(int(t.get("seeded_skipped") or 0) for t in tables)
        if seeded:
            click.echo(f"  Kept, not loaded again: {_plural(seeded, 'row')} the engine had already written for the tenant")
        for t in tables:
            if int(t.get("dropped") or 0):
                click.echo(f"  {t.get('table')}: {_plural(int(t['dropped']), 'row')} dropped (the row they point at was gone before the backup)")
    elif out.marker is not None:
        click.echo(
            "The engine's own report is gone (it restarted or lost the session); its restore marker records that this "
            f"file's load committed (session {out.marker.get('session_id', '?')}, {out.marker.get('committed_at', '?')})."
        )
        counts = out.marker.get("row_counts")
        if isinstance(counts, dict) and counts:
            total = sum(int(v) for v in counts.values() if isinstance(v, int))
            click.echo(f"  {_plural(total, 'row')} in {_plural(len(counts), 'table')} per the marker")
    skipped_report = report.get("skipped_tables")
    skipped = {**result.skipped_at_open, **(skipped_report if isinstance(skipped_report, dict) else {})}
    for name, why in sorted(skipped.items()):
        click.echo(f"  Skipped table {name}: {why}")
    columns = report.get("skipped_columns")
    for col in columns if isinstance(columns, list) else []:
        click.echo(f"  Skipped column {col} (retired by a later schema)")
    softs = report.get("soft_references")
    for s in softs if isinstance(softs, list) else []:
        remapped, passed, negated = (int(s.get(k) or 0) for k in ("remapped", "passed_through", "negated"))
        if remapped or passed or negated:
            why = " (the rows they named were gone before the backup)" if negated else ""
            click.echo(f"  {s.get('column')}: {remapped} remapped, {passed} passed through, {negated} negated{why}")
    for note in dict.fromkeys([*result.open_notes, *out.notes]):
        click.echo(f"  Note: {note}")
