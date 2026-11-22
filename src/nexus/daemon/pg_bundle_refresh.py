# SPDX-License-Identifier: AGPL-3.0-or-later
"""Install the pinned PostgreSQL bundle and swap it in; the convergence leg that does it.

An install keeps the PG bundle it was first set up with: ``nx upgrade`` installed the engine
binary and never touched ``<config>/pg-bundle``. pg_background first shipped in the bundle at
engine-service-v0.1.190, so an install first set up before that reached a newer engine with a
bundle that lacks it, and every RDR-246 data move stayed blocked (nexus-f3yqd.2.23).

Two things live here:

* :func:`install_and_extract_pg_bundle`: download the bundle of a tag, verify and place the
  archive, then extract it under a stop of the stack. ``nx daemon service install-binary`` calls
  it; convergence calls its two halves (:func:`fetch_pg_bundle`, :func:`swap_in_pg_bundle`) so the
  download and the staging extraction finish before anything stops.
* the convergence leg, which runs when the box is local, runs an extracted bundle and either the
  engine changes (:func:`refresh_bundle_alongside_engine`) or the engine is already the pinned one and
  the bundle lacks pg_background (:func:`refresh_when_pg_background_is_missing`). A failure leaves
  the old bundle in place and is reported, never raised.

This is a binary swap. No user data is rewritten: a data directory made by PostgreSQL 17 starts
under any other 17 bundle, and :func:`nexus.db.pg_bundle.extract_bundle` refuses a swap across a
major version, or one that would drop pg_background, before anything is stopped.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

_log = structlog.get_logger(__name__)

#: ``(config_dir, tag)`` -> ``(why, refused)`` for the rest of this process. The finish pass can call
#: engine convergence twice in one invocation; a failed download or a refused swap is not repeated
#: (the same rule as ``upgrade_finish._ENGINE_INSTALL_FAILURES``). The next ``nx`` run retries a failure
#: (``refused`` False); a refusal is deterministic for the archive's bytes, and the archive stays placed, so the
#: next run re-checks it without a download (:func:`_placed_archive`).
_REFRESH_FAILURES: dict[tuple[str, str], tuple[str, bool]] = {}


def bundle_has_pg_background(extract_root: Path) -> bool:
    from nexus.db import pg_bundle  # noqa: PLC0415 - deferred, CLI startup cost

    return pg_bundle.bundle_has_pg_background(extract_root)


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
    ``lzma.LZMAError``, :class:`~nexus.db.pg_bundle.PgBundleSwapRefused`) propagate as they
    are. *quiesce* is the stop that guards the swap: a
    :class:`~nexus.daemon.replace_quiesce.SharedQuiesce` guard, so a caller that also
    replaces the engine stops and starts the stack once. A bundle with the bytes already
    extracted enters nothing and stops nothing. *on_installed* runs between the two steps.
    """
    archive, provenance = fetch_pg_bundle(tag, config_dir, installed_by=installed_by, on_installed=on_installed)
    return archive, provenance, swap_in_pg_bundle(archive, config_dir, quiesce)


def fetch_pg_bundle(
    tag: str,
    config_dir: Path,
    *,
    installed_by: str,
    on_installed: Callable[[Path, dict], None] | None = None,
) -> tuple[Path, dict]:
    """The download half: fetch, verify and place the archive. Stops nothing."""
    # Module attribute, read at call time: the callers' tests replace this seam.
    from nexus.daemon import binary_install  # noqa: PLC0415 - deferred, CLI startup cost

    archive, provenance = binary_install.install_pg_bundle(tag, config_dir, installed_by=installed_by)
    if on_installed is not None:
        on_installed(archive, provenance)
    return archive, provenance


def swap_in_pg_bundle(
    archive: Path, config_dir: Path, quiesce: AbstractContextManager[object] | None,
) -> Path:
    """The extraction half: extract *archive* (a staged tree from ``pg_bundle.prestage_bundle`` is
    reused) and swap it in under *quiesce*. Returns the ``bin/`` directory."""
    from nexus.db import pg_bundle  # noqa: PLC0415 - deferred, CLI startup cost

    return pg_bundle.extract_bundle(archive, pg_bundle.bundle_extract_root(config_dir), quiesce=quiesce)


@dataclass(frozen=True)
class BundleRefresh:
    """What a bundle refresh did: ``refreshed`` when a different bundle was swapped in, ``problem``
    the reason when it could not be, and ``next_step`` what an operator can do about it (empty when
    nothing a person does changes the answer)."""

    refreshed: bool = False
    problem: str | None = None
    next_step: str = ""

    def lines(self) -> list[str]:
        if self.refreshed:
            return ["refreshed the PostgreSQL bundle (a different bundle was extracted and swapped in)"]
        if self.problem:
            tail = f" {self.next_step}" if self.next_step else ""
            return [
                f"NOTE: the PostgreSQL bundle was not refreshed: {self.problem.rstrip('. ')}. The bundle in place is "
                f"unchanged and the stack keeps running on it.{tail}"
            ]
        return []


@dataclass
class PreparedBundle:
    """A refresh with its download and staging done and nothing stopped. ``done`` is set when
    there is nothing left to apply (not applicable, refused, failed, already current)."""

    tag: str = ""
    archive: Path | None = None
    done: BundleRefresh | None = None
    lacked: bool = False


def refresh_applicable(config_dir: Path) -> bool:
    """True for a local install that runs a bundle extracted under *config_dir*.

    Cloud mode has no local PostgreSQL, and an install on its own PostgreSQL has no
    extracted bundle; neither is touched."""
    from nexus.config import is_local_mode  # noqa: PLC0415 - deferred for test patchability
    from nexus.db import pg_bundle  # noqa: PLC0415 - deferred, CLI startup cost

    return is_local_mode() and pg_bundle.is_bundle_extracted(pg_bundle.bundle_extract_root(config_dir))


def bundle_lacks_pg_background(config_dir: Path) -> bool:
    from nexus.db import pg_bundle  # noqa: PLC0415 - deferred, CLI startup cost

    return refresh_applicable(config_dir) and not pg_bundle.bundle_has_pg_background(
        pg_bundle.bundle_extract_root(config_dir)
    )


def _pinned_bundle_in_place(config_dir: Path, tag: str) -> bool:
    """The extracted tree was made from the bundle of *tag* (the sidecar of the installed archive names
    the tag, and the extraction marker names that archive's digest). Reads two small files."""
    from nexus.daemon import binary_install  # noqa: PLC0415 - deferred, CLI startup cost
    from nexus.db import pg_bundle  # noqa: PLC0415 - deferred, CLI startup cost

    try:
        meta = json.loads((config_dir / "service" / binary_install._PG_SIDECAR_NAME).read_text(encoding="utf-8"))  # noqa: SLF001
        marker = (pg_bundle.bundle_extract_root(config_dir) / pg_bundle._EXTRACT_MARKER).read_text(encoding="utf-8")  # noqa: SLF001
    except (OSError, ValueError):
        return False
    return isinstance(meta, dict) and meta.get("tag") == tag and marker == f"sha256={meta.get('sha256')}\n"


def _placed_archive(config_dir: Path, tag: str) -> Path | None:
    """The archive already placed for *tag* (the sidecar names the tag and the digest it was verified with, and the
    file still has that digest), or ``None``. A refused or deferred refresh re-checks it without downloading again."""
    from nexus.daemon import binary_install  # noqa: PLC0415 - deferred, CLI startup cost

    try:
        meta = json.loads((config_dir / "service" / binary_install._PG_SIDECAR_NAME).read_text(encoding="utf-8"))  # noqa: SLF001
        archive = binary_install.pg_bundle_dest(config_dir)
        if meta.get("tag") == tag and archive.is_file() and binary_install.compute_sha256(archive) == meta.get("sha256"):
            return archive
    except (OSError, ValueError, AttributeError):
        pass
    return None


def _lacks_text(tag: str) -> str:
    return (
        f"the PostgreSQL bundle of {tag} does not contain pg_background, so data moves stay blocked: the "
        "release's own bundle is missing it"
    )


def prepare_refresh(tag: str, config_dir: Path) -> PreparedBundle:
    """Download and stage *tag*'s bundle with the stack still running; never raises.

    The return carries what :func:`apply_refresh` still has to do, or ``done`` when nothing is
    left. A bundle that lacks pg_background and was already made from this tag is not downloaded
    again: the release's own bundle is what is missing it."""
    from nexus.db import pg_bundle  # noqa: PLC0415 - deferred, CLI startup cost

    prep = PreparedBundle(tag=tag)
    if not refresh_applicable(config_dir):
        prep.done = BundleRefresh()
        return prep
    root = pg_bundle.bundle_extract_root(config_dir)
    prep.lacked = not pg_bundle.bundle_has_pg_background(root)
    if prep.lacked and _pinned_bundle_in_place(config_dir, tag):
        prep.done = BundleRefresh(problem=_lacks_text(tag))
        return prep
    key = (str(config_dir), tag)
    if key in _REFRESH_FAILURES:
        why, refused = _REFRESH_FAILURES[key]
        prep.done = BundleRefresh(
            problem=f"{why.rstrip('. ')} (already tried in this run)",
            next_step="" if refused else _retry_text(config_dir, tag),
        )
        return prep
    try:
        archive = _placed_archive(config_dir, tag)
        if archive is None:
            archive, _prov = fetch_pg_bundle(tag, config_dir, installed_by="upgrade-finish engine convergence")
        swaps = pg_bundle.prestage_bundle(archive, root)
        if swaps and prep.lacked and not pg_bundle.bundle_has_pg_background(pg_bundle.staging_dir(root)):
            pg_bundle.discard_staging(root)  # lacking for lacking: a restart of PostgreSQL for nothing
            prep.done = BundleRefresh(problem=_lacks_text(tag))
            return prep
    except pg_bundle.PgBundleSwapRefused as exc:
        _log.warning("pg_bundle_refresh_refused", tag=tag, error=str(exc))
        _REFRESH_FAILURES[key] = (str(exc), True)
        prep.done = BundleRefresh(problem=str(exc))
        return prep
    except Exception as exc:  # noqa: BLE001 - "never raises": every failure here leaves the old bundle
        _log.warning("pg_bundle_refresh_failed", tag=tag, stage="prepare", error=str(exc), exc_info=True)
        _REFRESH_FAILURES[key] = (str(exc), False)
        prep.done = BundleRefresh(problem=str(exc), next_step=_retry_text(config_dir, tag))
        return prep
    prep.archive = archive
    return prep


def _retry_text(config_dir: Path, tag: str) -> str:
    """The command that retries a failed refresh: `nx upgrade` while the bundle lacks pg_background
    (the convergence leg fires on that), else the explicit install."""
    if bundle_lacks_pg_background(config_dir):
        return "Run `nx upgrade` again to try again."
    return f"`nx daemon service install-binary {tag}` tries again."


def apply_refresh(
    prep: PreparedBundle, config_dir: Path, quiesce: AbstractContextManager[object] | None,
) -> BundleRefresh:
    """Swap the prepared bundle in under *quiesce*; never raises. A swap that fails leaves the old
    bundle in place. After it, pg_background is looked for: a bundle that still lacks it is reported."""
    from nexus.db import pg_bundle  # noqa: PLC0415 - deferred, CLI startup cost

    if prep.done is not None:
        return prep.done
    assert prep.archive is not None
    root = pg_bundle.bundle_extract_root(config_dir)
    marker = root / pg_bundle._EXTRACT_MARKER  # noqa: SLF001 - the module's own marker
    before = marker.read_text(encoding="utf-8") if marker.is_file() else ""
    try:
        swap_in_pg_bundle(prep.archive, config_dir, quiesce)
    except pg_bundle.PgBundleSwapRefused as exc:
        _log.warning("pg_bundle_refresh_refused", tag=prep.tag, error=str(exc))
        _REFRESH_FAILURES[(str(config_dir), prep.tag)] = (str(exc), True)
        return BundleRefresh(problem=str(exc))
    except Exception as exc:  # noqa: BLE001 - "never raises": every failure here leaves the old bundle
        _log.warning("pg_bundle_refresh_failed", tag=prep.tag, stage="swap", error=str(exc), exc_info=True)
        _REFRESH_FAILURES[(str(config_dir), prep.tag)] = (str(exc), False)
        return BundleRefresh(problem=str(exc), next_step=_retry_text(config_dir, prep.tag))
    refreshed = (marker.read_text(encoding="utf-8") if marker.is_file() else "") != before
    if (prep.lacked or refreshed) and not pg_bundle.bundle_has_pg_background(root):
        return BundleRefresh(refreshed=False, problem=_lacks_text(prep.tag))
    return BundleRefresh(refreshed=refreshed)


def refresh_bundle_alongside_engine(
    tag: str, config_dir: Path, install_engine: Callable[[AbstractContextManager[Any]], object],
    *, unattended: bool = False,
) -> BundleRefresh:
    """The engine-change path: the bundle is downloaded and staged first, with the stack still up;
    then ONE stop covers the engine swap (*install_engine*, handed its guard) and the bundle swap,
    and nothing starts until the caller's restart-and-verify does. An exception from
    *install_engine* propagates (the stack is started again, as before); the bundle's never does.

    A bundle swap stops PostgreSQL, which an engine replacement alone does not, and that kills an index
    build in flight. So an *unattended* run, or one that finds the engine busy (:func:`engine_busy_reason`),
    installs the engine as it always did and leaves the bundle alone: the swap is reported pending, and the
    doctor row and the next attended ``nx upgrade`` (the converged-engine leg) take it."""
    from nexus.daemon.replace_quiesce import SharedQuiesce  # noqa: PLC0415 - deferred, CLI startup cost
    from nexus.db import pg_bundle  # noqa: PLC0415 - deferred, CLI startup cost

    hold = _hold_reason(config_dir, unattended)
    if hold is not None:
        prep = PreparedBundle(tag=tag, done=_pending(config_dir, tag, hold))
    else:
        prep = prepare_refresh(tag, config_dir)
    try:
        with SharedQuiesce(config_dir, restart_after=False) as shared:
            install_engine(shared.guard("engine"))
            return apply_refresh(prep, config_dir, shared.guard("pg_bundle"))
    finally:
        pg_bundle.discard_staging(pg_bundle.bundle_extract_root(config_dir))


def _hold_reason(config_dir: Path, unattended: bool) -> str | None:
    """Why a bundle swap must not run now, for a local install with an extracted bundle: an unattended run, or
    a busy engine. ``None`` when it may run (or there is no bundle to swap)."""
    if not refresh_applicable(config_dir):
        return None
    if unattended:
        return "this is an unattended run, and replacing the bundle restarts PostgreSQL"
    busy = engine_busy_reason(config_dir, moves_can_run=not bundle_lacks_pg_background(config_dir))
    return f"{busy}; replacing the bundle restarts PostgreSQL, which stops that work" if busy else None


def _pending(config_dir: Path, tag: str, why: str) -> BundleRefresh:
    """A bundle swap held back: said only when the bundle lacks pg_background (what the doctor row tracks)."""
    if not bundle_lacks_pg_background(config_dir):
        return BundleRefresh()
    return BundleRefresh(
        problem=f"held back: {why}",
        next_step=(
            "Run `nx upgrade` when the engine is idle (or "
            f"`nx daemon service install-binary {tag}` to do it now); `nx doctor` has a PostgreSQL bundle row until then."
        ),
    )


# -- the idle gate of the converged-engine leg ---------------------------------------------------------


def _engine_status() -> dict | None:
    from nexus.db.http_engine_status import fetch_engine_status  # noqa: PLC0415 - deferred, CLI startup cost

    return fetch_engine_status()


def _service_up(config_dir: Path) -> bool:
    from nexus.db.service_endpoint import discover_lease  # noqa: PLC0415 - deferred, CLI startup cost

    return discover_lease()[0] is not None


def _progress_rows(config_dir: Path) -> int | None:
    """Rows in ``pg_stat_progress_create_index`` and ``pg_stat_progress_vacuum`` (an index build or a
    vacuum in flight), or ``None`` when they cannot be read."""
    try:
        from nexus.db.pg_provision import CREDENTIALS_FILENAME, _read_credentials, discover_pg_binaries  # noqa: PLC0415 - deferred, heavy
        from nexus.health import _run_psql  # noqa: PLC0415 - deferred, heavy

        creds = _read_credentials(config_dir / CREDENTIALS_FILENAME)
        url = creds.get("NX_DB_ADMIN_URL", "")
        proc = _run_psql(
            discover_pg_binaries(config_dir).psql, "127.0.0.1", int(creds["PG_PORT"]),
            url.rstrip("/").rsplit("/", 1)[-1] if "/" in url else "nexus",
            creds.get("NX_DB_ADMIN_USER", "nexus_admin"), creds.get("NX_DB_ADMIN_PASS", ""),
            "SELECT (SELECT count(*) FROM pg_stat_progress_create_index) + "
            "(SELECT count(*) FROM pg_stat_progress_vacuum);",
            timeout=20.0,
        )
        return int(proc.stdout.strip()) if proc.returncode == 0 else None
    except Exception as exc:  # noqa: BLE001 - unreadable is reported as unknown, never as idle
        _log.debug("pg_bundle_refresh_progress_probe_failed", error=str(exc))
        return None


def _is_parked_move(entry: dict) -> bool:
    """A data-move entry whose state is ``blocked:``, ``failed:`` or ``paused:``: the engine reports every open move
    in ``boot_work``, and one of these is waiting, not working (a move blocked on a missing pg_background is exactly
    what the bundle swap fixes, so it must not defer the swap). ``working:``, ``queued:``, ``stalled:`` and every
    entry that is not a move (index builds, the worker's own) are work."""
    what = str(entry.get("what") or "")
    return _is_data_move(entry) and what.partition(":")[0].strip().lower() in ("blocked", "failed", "paused")


def _is_data_move(entry: dict) -> bool:
    return str(entry.get("id") or "").startswith("data-move:")


#: The engine's ``boot_work`` id of the RDR-229 restart notice (``CompletionBootWork.RESTART_ENTRY_ID``): the stamps are
#: complete and a P3.5b changeset waits for one engine restart. It stays visible (``nx doctor``, ``nx daemon service status``)
#: and is safe to stop, but it is not work: the bundle swap this gate guards IS a restart, and so is the engine replacement of
#: the same ``nx upgrade``, so counting it would defer the very restart that clears it (linda 2026-11-22, nexus-p2m1x.27).
RESTART_NOTICE_ID = "rdr229-restart"


def _is_restart_notice(entry: dict) -> bool:
    return str(entry.get("id") or "") == RESTART_NOTICE_ID


def engine_busy_reason(config_dir: Path, *, moves_can_run: bool = True) -> str | None:
    """Why the stack should not be bounced now, or ``None`` when it is idle.

    The idle rule of ``scripts/upgrade_boot_writes.py``: ``/v1/status`` ``boot_work`` empty and no
    index build or vacuum running. A service that is not running has nothing in flight. A running
    one whose state cannot be read is not idle: unknown is not idle.

    ``moves_can_run=False`` is for a bundle that lacks pg_background: no data move can run on it, so a
    ``queued:`` or ``working:`` move is waiting for the very swap this gate guards, and counting it as work
    would defer the swap for ever (F33: from 8.2.0 the registered moves were ``queued:`` at the attended
    ``nx upgrade`` and its bundle swap was deferred on them). Index builds and other entries still count; the RDR-229 restart notice (:data:`RESTART_NOTICE_ID`) does not."""
    status = _engine_status()
    if status is None:
        if not _service_up(config_dir):
            return None
        return "the engine is running but its status cannot be read, so whether it is building indexes is unknown"
    work = [
        e for e in (status.get("boot_work") or [])
        if isinstance(e, dict) and not _is_parked_move(e) and not _is_restart_notice(e)
        and (moves_can_run or not _is_data_move(e))
    ]
    if work:
        names = ", ".join(sorted({str(e.get("what") or e.get("id") or "work")[:60] for e in work}))
        return f"the engine reports background work ({names or 'work in progress'})"
    rows = _progress_rows(config_dir)
    if rows is None:
        return "the engine is running but its index builds cannot be read, so whether one is in flight is unknown"
    if rows:
        return f"{rows} index build or vacuum is running"
    return None


def refresh_when_pg_background_is_missing(
    tag: str | None, config_dir: Path, *, dry_run: bool, acquire: bool, unattended: bool = False,
) -> list[str]:
    """The convergence leg for an engine that is already the pinned one: a bundle without
    pg_background is replaced. ``[]`` when there is nothing to do.

    ``dry_run``, ``acquire=False`` (the report-only passes) and ``unattended`` say what is missing and
    change nothing: this leg stops PostgreSQL, which kills an index build in flight, so it never runs
    unprompted. It also defers while the engine reports boot work or an index build. Otherwise the
    stack is stopped once for the swap and started again; a restart that fails is reported, never raised."""
    if not bundle_lacks_pg_background(config_dir):
        return []
    if dry_run:
        return ["pending: the installed PostgreSQL bundle lacks pg_background; `nx upgrade` replaces it (it stops and restarts PostgreSQL and the storage service)"]
    if not acquire or unattended:
        return [
            "NOTE: the installed PostgreSQL bundle lacks pg_background, so data moves stay blocked. "
            "Nothing was changed. Run `nx upgrade` when you can spare a few minutes: it replaces the "
            "bundle and restarts PostgreSQL and the storage service."
        ]
    if not tag:
        return ["NOTE: the installed PostgreSQL bundle lacks pg_background and no pinned service tag is configured."]
    busy = engine_busy_reason(config_dir, moves_can_run=False)  # this leg runs only for a bundle without pg_background
    if busy:
        return [
            f"NOTE: the PostgreSQL bundle lacks pg_background; replacing it is deferred: {busy}. Replacing it "
            f"restarts PostgreSQL, which stops that work. Run `nx upgrade` again when the engine is idle, or "
            f"`nx daemon service install-binary {tag}` to do it now."
        ]
    from nexus.daemon.replace_quiesce import RestartAfterReplaceError, SharedQuiesce  # noqa: PLC0415 - deferred, CLI startup cost
    from nexus.db import pg_bundle  # noqa: PLC0415 - deferred, CLI startup cost

    prep = prepare_refresh(tag, config_dir)  # the download and the staging, with the stack still up
    shared = SharedQuiesce(config_dir)
    result: BundleRefresh | None = None
    try:
        with shared:
            result = apply_refresh(prep, config_dir, shared.guard("pg_bundle"))
    except RestartAfterReplaceError as exc:
        done = result.lines() if result is not None else []
        if exc.slow_start:  # the supervisor is alive and the engine has not answered yet: it may be migrating
            return done + [
                f"NOTE: the PostgreSQL bundle was refreshed and the stack is still starting: {exc} "
                "Check `nx daemon service status` and `nx doctor`."
            ]
        return done + [
            f"NEEDS HUMAN: the PostgreSQL bundle was refreshed but the stack could not be started again: {exc}"
        ]
    except Exception as exc:  # noqa: BLE001 - "never raises"
        _log.warning("pg_bundle_refresh_stack_failed", error=str(exc), exc_info=True)
        return [f"NOTE: the PostgreSQL bundle was not refreshed: {exc}"]
    finally:
        pg_bundle.discard_staging(pg_bundle.bundle_extract_root(config_dir))
    return result.lines()


def check_pg_bundle(config_dir: Path | None = None) -> list[Any]:
    """``nx doctor``'s row for the extracted PostgreSQL bundle: a warning while a local install's bundle lacks
    pg_background. Nothing for cloud mode, an install on its own PostgreSQL, or a bundle that has it."""
    import nexus.config as _config  # noqa: PLC0415 - deferred, CLI startup cost
    from nexus.engine_version import REQUIRED_ENGINE_VERSION  # noqa: PLC0415 - deferred, CLI startup cost
    from nexus.health import HealthResult  # noqa: PLC0415 - deferred: that module is heavy

    config_dir = config_dir if config_dir is not None else _config.nexus_config_dir()
    if not bundle_lacks_pg_background(config_dir):
        return []
    tag = "engine-service-v" + ".".join(str(p) for p in REQUIRED_ENGINE_VERSION)
    if _pinned_bundle_in_place(config_dir, tag):
        return [HealthResult(
            label="PostgreSQL bundle", ok=False, warn=True,
            detail="the extracted PostgreSQL bundle has no pg_background, and it is the one this release pins",
            fix_suggestions=[f"The release's own bundle ({tag}) is missing the extension: report it. Data moves stay blocked until a bundle has it"],
        )]
    return [HealthResult(
        label="PostgreSQL bundle", ok=False, warn=True,
        detail="the extracted PostgreSQL bundle has no pg_background, so every registered data move stays blocked",
        fix_suggestions=["nx upgrade replaces the bundle (it restarts PostgreSQL and the storage service; `nx upgrade --dry-run` lists it)"],
    )]
