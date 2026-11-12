# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which dump counts as the copy the legacy-chash guard waits for (nexus-f3yqd).

A dump clears the refusal only when it is a COMPLETE dump of THIS cluster taken after the refusal:

1. ``pg_restore -l`` (the table of contents, which sits at the start of a custom-format dump) lists the marker schema
   the refusal created and the table data the engine would delete;
2. ``pg_restore -f <devnull> -a`` reads every data block to the end. A listing alone is not proof: a dump still being
   written, or truncated by a killed copy, lists everything and fails only when its data is read.

Nothing here runs ``nx backup`` or imports its module (``tests/test_nx_backup_pin.py``).
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import structlog

_log = structlog.get_logger(__name__)

#: nx backup's default directory under the home directory (its own constant cannot be imported: the backup pin).
BACKUP_DIRNAME: str = "nexus-backups"
SENTINEL_PREFIX: str = "nx_legacy_guard_"
SENTINEL_NAME = re.compile(rf"{SENTINEL_PREFIX}[0-9a-f]{{12}}")
MANIFEST: str = "catalog_document_chunks"
#: One ``pg_restore -l`` gets this long; one full read gets this long (a big dump is read at disk speed); one attempt
#: looks at this many dumps, newest first (cost bound).
RESTORE_LIST_TIMEOUT_S: float = 60.0
RESTORE_READ_TIMEOUT_S: float = 1800.0
MAX_DUMPS_PER_ATTEMPT: int = 5
#: Exits that say the tool could not run, not that the dump is bad: the shell's "cannot execute" and "not found", and
#: Windows' STATUS_DLL_NOT_FOUND (0xC0000135) as an unsigned and as a signed exit code.
UNAVAILABLE_EXITS: frozenset[int] = frozenset({126, 127, 0xC0000135, -1073741515})

Runner = Callable[..., subprocess.CompletedProcess]


class PgRestoreUnavailable(RuntimeError):
    """``pg_restore`` could not be run, so no dump could be read: the user's dump is not at fault."""


def backup_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / BACKUP_DIRNAME


def _lists_table_data(listing: str, schema: str, table: str, *, prefix_ok: bool = False) -> bool:
    tail = r"(?:_\S+)?" if prefix_ok else ""
    return bool(re.search(rf"TABLE DATA {re.escape(schema)} {re.escape(table)}{tail}(?:\s|$)", listing))


def holds_required_data(listing: bytes | str, record: dict) -> bool:
    """Does this ``pg_restore -l`` listing show a dump of THIS cluster (the marker schema) that holds the table data the
    engine would delete?"""
    text = listing.decode("utf-8", "replace") if isinstance(listing, bytes) else listing
    sentinel = record.get("sentinel")
    if not sentinel or not SENTINEL_NAME.fullmatch(sentinel) or not re.search(
        rf"\bSCHEMA - {re.escape(sentinel)}(?:\s|$)", text
    ):
        return False
    required = record.get("required") or [[MANIFEST, False]]
    return all(_lists_table_data(text, "nexus", t, prefix_ok=bool(p)) for t, p in required)


def _restore_env(pg_restore: Path) -> dict[str, str]:
    from nexus.db.pg_provision import _bundle_lib_env  # noqa: PLC0415 — deferred: heavy import

    return _bundle_lib_env([str(pg_restore)], None)


def dump_key(dump: Path) -> str:
    stat = dump.stat()
    return f"{dump}|{stat.st_mtime_ns}|{stat.st_size}"


def rejected_paths(record: dict) -> list[tuple[str, str]]:
    """The dumps already checked and not accepted, as ``(path, reason)``."""
    return [(key.rsplit("|", 2)[0], reason) for key, reason in (record.get("rejected") or {}).items()]


def _run(runner: Runner, argv: list[str], timeout: float, env: dict[str, str], pg_restore: Path):
    """One ``pg_restore`` call: the completed process, or None on a timeout. A tool that cannot run raises."""
    try:
        done = runner(argv, timeout=timeout, env=env, text=False)
    except subprocess.TimeoutExpired:
        _log.warning("legacy_chash_guard_backup_read_timed_out", argv=argv[-1], timeout_s=timeout)
        return None
    except (OSError, subprocess.SubprocessError) as exc:
        raise PgRestoreUnavailable(f"{pg_restore}: {exc}") from exc
    if done.returncode in UNAVAILABLE_EXITS:
        tail = done.stderr or b""
        tail = tail.decode("utf-8", "replace") if isinstance(tail, bytes) else tail
        raise PgRestoreUnavailable(f"{pg_restore} exited {done.returncode}: {tail.strip()[-200:]}")
    return done


def verified_backup_since(
    record: dict, *, pg_restore: Path, home: Path | None = None, explicit: Path | None = None,
    runner: Runner | None = None,
) -> Path | None:
    """The newest dump written after the refusal that lists the marker schema and the data the engine would delete AND
    reads back to its end, or None. Looks in ``~/nexus-backups/``, or only at ``explicit`` (``--legacy-backup``). A dump
    already rejected (same path, mtime and size) is not read again; one attempt reads at most ``MAX_DUMPS_PER_ATTEMPT``.
    Raises :class:`PgRestoreUnavailable` when ``pg_restore`` cannot run, which says nothing about any dump."""
    if runner is None:
        from nexus.bounded_subprocess import run_bounded  # noqa: PLC0415 — deferred

        runner = run_bounded
    try:
        since = datetime.fromisoformat(record["refused_at"]).timestamp()
    except (KeyError, ValueError):
        return None
    rejected: dict = record.setdefault("rejected", {})
    try:
        pool = [explicit] if explicit is not None else list(backup_dir(home).glob("*.dump"))
        candidates = sorted((p for p in pool if p.is_file() and p.stat().st_mtime > since),
                            key=lambda p: p.stat().st_mtime, reverse=True)
        candidates = [p for p in candidates if dump_key(p) not in rejected][:MAX_DUMPS_PER_ATTEMPT]
    except OSError:
        return None
    env = _restore_env(pg_restore)
    for dump in candidates:
        listing = _run(runner, [str(pg_restore), "-l", str(dump)], RESTORE_LIST_TIMEOUT_S, env, pg_restore)
        if listing is None:
            continue
        if listing.returncode != 0:
            reason = "unreadable"
        elif not holds_required_data(listing.stdout, record):
            reason = "does not hold this store's marker schema and chunk data"
        else:
            read = _run(runner, [str(pg_restore), "-f", os.devnull, "-a", str(dump)], RESTORE_READ_TIMEOUT_S, env,
                        pg_restore)
            if read is None:
                continue  # too slow to settle now: neither accepted nor written off
            if read.returncode == 0:
                return dump
            reason = "incomplete: pg_restore could not read all of its data"
        rejected[dump_key(dump)] = reason
        _log.warning("legacy_chash_guard_backup_rejected", path=str(dump), returncode=listing.returncode,
                     reason=reason)
    return None
