# SPDX-License-Identifier: AGPL-3.0-or-later
"""Install the pinned PostgreSQL bundle and swap it in; the convergence leg that does it.

An install keeps the PG bundle it was first set up with: ``nx upgrade`` installed the engine
binary and never touched ``<config>/pg-bundle``. pg_background first shipped in the bundle at
engine-service-v0.1.190, so an install first set up before that reached a newer engine with a
bundle that lacks it, and every RDR-246 data move stayed blocked (nexus-f3yqd.2.23).

Two things live here:

* :func:`install_and_extract_pg_bundle`: download the bundle of a tag, verify and place the
  archive, then extract it under a stop of the stack. ``nx daemon service install-binary`` and
  engine convergence both call it, so there is one path and one set of error rules.
* the convergence leg (:func:`refresh_for_convergence`, :func:`refresh_when_pg_background_is_missing`):
  when the box is local, runs an extracted bundle and either the engine changes or the bundle
  lacks pg_background, take the pinned tag's bundle and swap it in. A failure leaves the old
  bundle in place and is reported, never raised.

This is a binary swap. No user data is rewritten: a data directory made by PostgreSQL 17 starts
under any other 17 bundle, and a swap across a major version is refused by
:func:`nexus.db.pg_bundle.extract_bundle` before anything is stopped.
"""
from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

import structlog

_log = structlog.get_logger(__name__)

#: ``(config_dir, tag)`` -> why the refresh failed, for the rest of this process. The finish
#: pass can call engine convergence twice in one invocation; a failed download is not repeated
#: (the same rule as ``upgrade_finish._ENGINE_INSTALL_FAILURES``). The next ``nx`` run retries.
_REFRESH_FAILURES: dict[tuple[str, str], str] = {}


def pg_background_control_files(extract_root: Path) -> tuple[Path, ...]:
    """Where the extension's control file sits in an extracted bundle.

    The real layout puts it beside ``vector.control`` at
    ``bundle/share/postgresql/extension/`` (PostgreSQL adds ``postgresql`` to the share
    directory when the prefix does not name it). A build that does not add it is covered by
    the second entry."""
    share = extract_root / "bundle" / "share"
    return (
        share / "postgresql" / "extension" / "pg_background.control",
        share / "extension" / "pg_background.control",
    )


def bundle_has_pg_background(extract_root: Path) -> bool:
    return any(p.is_file() for p in pg_background_control_files(extract_root))


def install_and_extract_pg_bundle(
    tag: str,
    config_dir: Path,
    *,
    installed_by: str,
    quiesce: AbstractContextManager[object] | None,
    on_installed: Callable[[Path, dict], None] | None = None,
) -> tuple[Path, dict, Path]:
    """Download, verify and place *tag*'s PG bundle archive, then extract it.

    Returns ``(archive, provenance, bin_dir)``. ``binary_install.BinaryVerificationError``
    is the download or verification failing, before anything is stopped; the extraction's
    own errors (``ReplaceBlockedError``, ``RuntimeError``, ``OSError``, ``tarfile.TarError``,
    ``lzma.LZMAError``, :class:`~nexus.db.pg_bundle.PgBundleMajorChangeError`) propagate
    as they are. *quiesce* is the stop that guards the swap: a
    :class:`~nexus.daemon.replace_quiesce.SharedQuiesce` guard, so a caller that also
    replaces the engine stops and starts the stack once. A bundle with the bytes already
    extracted enters nothing and stops nothing. *on_installed* runs between the two steps.
    """
    # Module attributes, read at call time: the callers' tests replace these seams.
    from nexus.daemon import binary_install  # noqa: PLC0415 - deferred, CLI startup cost
    from nexus.db import pg_bundle  # noqa: PLC0415 - deferred, CLI startup cost

    archive, provenance = binary_install.install_pg_bundle(tag, config_dir, installed_by=installed_by)
    if on_installed is not None:
        on_installed(archive, provenance)
    bin_dir = pg_bundle.extract_bundle(
        archive, pg_bundle.bundle_extract_root(config_dir), quiesce=quiesce,
    )
    return archive, provenance, bin_dir


@dataclass(frozen=True)
class BundleRefresh:
    """What :func:`refresh_for_convergence` did: ``refreshed`` when a different bundle was
    swapped in, ``problem`` the reason when it could not be."""

    refreshed: bool = False
    problem: str | None = None

    def lines(self) -> list[str]:
        if self.refreshed:
            return ["refreshed the PostgreSQL bundle (a different bundle was extracted and swapped in)"]
        if self.problem:
            return [
                f"NOTE: the PostgreSQL bundle was not refreshed: {self.problem}. The bundle in place is "
                "unchanged and the stack keeps running on it; the next `nx upgrade` tries again."
            ]
        return []


def refresh_applicable(config_dir: Path) -> bool:
    """True for a local install that runs a bundle extracted under *config_dir*.

    Cloud mode has no local PostgreSQL, and an install on its own PostgreSQL has no
    extracted bundle; neither is touched."""
    from nexus.config import is_local_mode  # noqa: PLC0415 - deferred for test patchability
    from nexus.db import pg_bundle  # noqa: PLC0415 - deferred, CLI startup cost

    return is_local_mode() and pg_bundle.is_bundle_extracted(pg_bundle.bundle_extract_root(config_dir))


def bundle_lacks_pg_background(config_dir: Path) -> bool:
    from nexus.db import pg_bundle  # noqa: PLC0415 - deferred, CLI startup cost

    return refresh_applicable(config_dir) and not bundle_has_pg_background(pg_bundle.bundle_extract_root(config_dir))


def refresh_for_convergence(
    tag: str, config_dir: Path, quiesce: AbstractContextManager[object] | None,
) -> BundleRefresh:
    """Swap in *tag*'s bundle when the box is local and runs an extracted bundle.

    Never raises: a download, extraction or swap failure (and a refused major change) leaves
    the old bundle in place, is logged and comes back as ``problem``. Nothing is stopped
    before the download and the staging extraction succeed."""
    if not refresh_applicable(config_dir):
        return BundleRefresh()
    from nexus.db import pg_bundle  # noqa: PLC0415 - deferred, CLI startup cost

    key = (str(config_dir), tag)
    if key in _REFRESH_FAILURES:
        return BundleRefresh(problem=f"{_REFRESH_FAILURES[key]} (already tried in this run)")
    marker = pg_bundle.bundle_extract_root(config_dir) / pg_bundle._EXTRACT_MARKER  # noqa: SLF001 - the module's own marker
    before = marker.read_text(encoding="utf-8") if marker.is_file() else ""
    try:
        install_and_extract_pg_bundle(
            tag, config_dir, installed_by="upgrade-finish engine convergence", quiesce=quiesce,
        )
    except Exception as exc:  # noqa: BLE001 - "never raises": every failure here leaves the old bundle
        _log.warning("pg_bundle_refresh_failed", tag=tag, error=str(exc), exc_info=True)
        _REFRESH_FAILURES[key] = str(exc)
        return BundleRefresh(problem=str(exc))
    after = marker.read_text(encoding="utf-8") if marker.is_file() else ""
    return BundleRefresh(refreshed=after != before)


def refresh_when_pg_background_is_missing(
    tag: str | None, config_dir: Path, *, dry_run: bool, acquire: bool,
) -> list[str]:
    """The convergence leg for an engine that is already the pinned one: a bundle without
    pg_background is replaced. ``[]`` when there is nothing to do.

    ``dry_run`` and ``acquire=False`` (the report-only passes) say what is missing and change
    nothing. Otherwise the stack is stopped once for the swap and started again, and a
    restart that fails is reported, never raised."""
    if not bundle_lacks_pg_background(config_dir):
        return []
    if dry_run:
        return ["would refresh the PostgreSQL bundle: the one installed lacks pg_background"]
    if not acquire:
        return [
            "NOTE: the installed PostgreSQL bundle lacks pg_background, so data moves stay blocked. "
            "Nothing was changed. Run `nx upgrade` when you can spare a few minutes: it replaces the "
            "bundle and restarts the storage service."
        ]
    if not tag:
        return ["NOTE: the installed PostgreSQL bundle lacks pg_background and no pinned service tag is configured."]
    from nexus.daemon.replace_quiesce import RestartAfterReplaceError, SharedQuiesce  # noqa: PLC0415 - deferred, CLI startup cost

    shared = SharedQuiesce(config_dir)
    try:
        with shared:
            result = refresh_for_convergence(tag, config_dir, shared.guard("pg_bundle"))
    except RestartAfterReplaceError as exc:
        return [
            f"NEEDS HUMAN: the PostgreSQL bundle was refreshed but the stack could not be started again: {exc}"
        ]
    except Exception as exc:  # noqa: BLE001 - "never raises"
        _log.warning("pg_bundle_refresh_stack_failed", error=str(exc), exc_info=True)
        return [f"NOTE: the PostgreSQL bundle was not refreshed: {exc}"]
    return result.lines()
