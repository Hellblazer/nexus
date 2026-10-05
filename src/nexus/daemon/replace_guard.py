# SPDX-License-Identifier: AGPL-3.0-or-later
"""Replacing installed files on Windows: bounded retry and all-or-nothing sets.

RDR-224 (nexus-f9bgu.20). Windows refuses to replace or rename a file or
directory that a running process holds open, and a virus scan or an indexer can
hold one for a moment after it was written. POSIX replaces a running binary
without complaint, so every function here takes the platform as an argument and
the POSIX arm is the single bare call the code made before this module existed.

Two pieces:

* :func:`replace_with_retry` is ``os.replace`` that, on Windows only, retries a
  ``PermissionError`` a bounded number of times and then raises
  :class:`ReplaceBlockedError` naming the path and the remedy.
* :func:`place_set_with_rollback` moves a set of staged files into a directory
  as one unit: every destination that already exists is first kept (hard link,
  falling back to a copy), each file is then swapped in with one atomic
  ``os.replace``, and a failure part way puts every file back the way it was.
  The set is never left half old and half new (new DLLs next to the old
  executable).

Stopping whatever holds the files is a separate concern, see
:mod:`nexus.daemon.replace_quiesce`.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path

import structlog

_log = structlog.get_logger(__name__)

#: Seconds slept between attempts. Six attempts, about 3.9 s in all: long enough
#: for a virus scan of a freshly written file to let go, short enough that a
#: file held by a process that is not going to exit fails visibly.
RETRY_DELAYS_S: tuple[float, ...] = (0.1, 0.25, 0.5, 1.0, 2.0)

_REMEDY = (
    "Stop the storage service (nx daemon service stop, add --with-pg for the "
    "PostgreSQL bundle) and run the command again. If nothing of nexus is "
    "running, a virus scanner or an open Explorer window on that folder is "
    "holding it: exclude the nexus config directory from scanning and retry."
)


class ReplaceBlockedError(OSError):
    """A file or directory could not be replaced because something holds it.

    Subclasses :class:`OSError` so every caller that already treats an install
    failure as an ``OSError`` keeps working; the message names the path and the
    remedy and is meant to be printed as it stands.
    """


def _is_windows(platform: str | None) -> bool:
    import sys  # noqa: PLC0415 - stdlib, kept local so the default stays patchable

    return (platform if platform is not None else sys.platform).startswith("win")


def replace_with_retry(
    src: str | os.PathLike[str],
    dst: str | os.PathLike[str],
    *,
    platform: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
    replace: Callable[[str | os.PathLike[str], str | os.PathLike[str]], object] = os.replace,
) -> None:
    """``os.replace(src, dst)``; on Windows a ``PermissionError`` is retried.

    POSIX: one call, exactly as before. Windows: up to ``len(RETRY_DELAYS_S) +
    1`` attempts, then :class:`ReplaceBlockedError` chained from the last
    ``PermissionError``. Any other error propagates at once; only a sharing
    violation is worth waiting for.
    """
    if not _is_windows(platform):
        replace(src, dst)
        return
    last: PermissionError | None = None
    for attempt in range(len(RETRY_DELAYS_S) + 1):
        try:
            replace(src, dst)
            return
        except PermissionError as exc:
            last = exc
            if attempt < len(RETRY_DELAYS_S):
                _log.info(
                    "replace_retry", dst=str(dst), attempt=attempt + 1, error=str(exc),
                )
                sleep(RETRY_DELAYS_S[attempt])
    raise ReplaceBlockedError(
        f"cannot replace {dst}: it is held open ({last}). {_REMEDY}"
    ) from last


def _keep_old(src: Path, dst: Path) -> None:
    """Keep *src*'s current content at *dst*: a hard link when the volume
    allows it (instant, even for a 190 MB executable), a copy when not.

    Measured on Windows 11 (nexus-f9bgu.20): with the old executable hard
    linked aside, ``os.replace`` over it SUCCEEDS while it runs, where a bare
    ``os.replace`` raises ``WinError 5``. That does not replace stopping first:
    the old process keeps running the old image, and the keep directory cannot
    be deleted until it exits (``.nx_old_*`` is left behind).
    """
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


#: A keep directory younger than this is left alone by the sweep: a placement
#: that is running right now owns it. A placement keeps its directory for
#: seconds (hard links, then renames), so a minute is far past any live one.
KEEP_DIR_MIN_AGE_S: float = 60.0

_KEEP_PREFIX = ".nx_old_"


def sweep_stale_keep_dirs(
    dest_dir: Path,
    *,
    now: Callable[[], float] = time.time,
    min_age_s: float = KEEP_DIR_MIN_AGE_S,
) -> list[Path]:
    """Remove the ``.nx_old_*`` keep directories an earlier placement left behind.

    A placement removes its own keep directory, but ``rmtree(ignore_errors=True)``
    fails silently when a process still runs from the hard-linked old executable,
    so every blocked upgrade used to leave one behind for good. The next
    SUCCESSFUL placement calls this: by then the files the old content belonged
    to are replaced, so it is of no use. Directories younger than *min_age_s* are
    skipped (a concurrent placement may own them). Never raises; returns the
    directories that are still there (a process still holds them), each logged.
    """
    left: list[Path] = []
    try:
        entries = list(dest_dir.iterdir())
    except OSError:
        return left
    cutoff = now() - min_age_s
    for entry in entries:
        if not entry.name.startswith(_KEEP_PREFIX):
            continue
        try:
            if not entry.is_dir() or entry.is_symlink() or entry.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        shutil.rmtree(entry, ignore_errors=True)
        if entry.exists():
            _log.warning("replace_keep_dir_not_removed", path=str(entry))
            left.append(entry)
    return left


def place_set_with_rollback(
    stage: Path,
    dest_dir: Path,
    names: Sequence[str],
    *,
    platform: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
    replace: Callable[[str | os.PathLike[str], str | os.PathLike[str]], object] = os.replace,
) -> None:
    """Move ``stage/<name>`` into ``dest_dir`` for every *name*, in order, as one set.

    Each file lands through one atomic ``os.replace`` (so a destination is never
    absent, even for an instant), the previous content of each destination is
    kept until the whole set is in, and a failure restores every destination:
    a replaced file gets its old content back, a first-time file is removed.
    Pass the names with the executable last, so the executable never sits next
    to a missing or older library because of this function.

    Raises :class:`ReplaceBlockedError` (the original error as ``__cause__``)
    when a file cannot be placed, after the rollback. When the rollback itself
    fails the old content is left in a ``.nx_old_*`` directory beside the
    destinations and the error names it and each file it could not restore.
    A success also sweeps the stale ``.nx_old_*`` directories earlier blocked
    placements left behind (:func:`sweep_stale_keep_dirs`).
    """
    keep = Path(tempfile.mkdtemp(dir=dest_dir, prefix=".nx_old_"))
    kept: set[str] = set()
    placed: list[str] = []
    try:
        for name in names:
            dest = dest_dir / name
            if dest.exists():
                _keep_old(dest, keep / name)
                kept.add(name)
            replace_with_retry(
                stage / name, dest, platform=platform, sleep=sleep, replace=replace,
            )
            placed.append(name)
    except BaseException as exc:
        failures = _roll_back(
            placed, kept, keep, dest_dir, platform=platform, sleep=sleep, replace=replace,
        )
        if failures:
            raise ReplaceBlockedError(
                f"replacing {', '.join(names)} in {dest_dir} failed ({exc}) and "
                f"restoring the previous files failed for {', '.join(failures)}. "
                f"The previous content is in {keep}: copy it back over the files "
                "before starting the service."
            ) from exc
        shutil.rmtree(keep, ignore_errors=True)
        if isinstance(exc, ReplaceBlockedError):
            raise
        if isinstance(exc, Exception):
            raise ReplaceBlockedError(
                f"replacing {', '.join(names)} in {dest_dir} failed: {exc}. "
                "The previous files are back in place."
            ) from exc
        raise
    shutil.rmtree(keep, ignore_errors=True)
    sweep_stale_keep_dirs(dest_dir)


def _roll_back(
    placed: Sequence[str],
    kept: set[str],
    keep: Path,
    dest_dir: Path,
    *,
    platform: str | None,
    sleep: Callable[[float], None],
    replace: Callable[[str | os.PathLike[str], str | os.PathLike[str]], object],
) -> list[str]:
    """Undo *placed* in reverse order. Returns the names that could not be undone."""
    failures: list[str] = []
    for name in reversed(placed):
        dest = dest_dir / name
        try:
            if name in kept:
                replace_with_retry(
                    keep / name, dest, platform=platform, sleep=sleep, replace=replace,
                )
            else:
                dest.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001 - a rollback step must not stop the next one
            _log.error("replace_rollback_failed", file=name, error=str(exc))
            failures.append(name)
    return failures
