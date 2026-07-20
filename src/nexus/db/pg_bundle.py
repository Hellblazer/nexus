# SPDX-License-Identifier: AGPL-3.0-or-later
"""First-run extraction of the ship-alongside PostgreSQL bundle (RDR-157 P3.4).

The local distribution ships a ``nexus-pg-<platform>.txz`` next to the native
binary (CA-2 verdict, bead nexus-vwvv5.11: ship-alongside, not embed). On first
run nexus locates that archive, extracts it once to a stable cache dir under the
config directory, and returns the bundle's ``bin/`` directory. The caller exports
that as ``NEXUS_PG_BIN`` so the existing
:func:`nexus.db.pg_provision.discover_pg_binaries` /
:func:`~nexus.db.pg_provision.provision` flow runs unchanged — the bundle is just
another binary location, selected ahead of host-PG discovery.

Relocation is safe: PostgreSQL resolves ``share``/``lib`` relative to the
executable via ``find_my_exec``, so the extracted tree works at any prefix (proven
end to end by ``tests/db/test_pg_bundle_relocation.py``). This module owns only the
LOCATE + idempotent EXTRACT + SELECT orchestration.

Artifact contract (RDR-157 P3.1, bead nexus-vwvv5.10): the ``.txz`` extracts to a
``bundle/`` tree containing ``bin/ include/ lib/ share/``; the CI artifact is named
``nexus-pg-<target>`` for targets ``mac-arm64`` / ``linux-amd64`` / ``linux-arm64``.
"""
from __future__ import annotations

import os
import platform
import shutil
import sys
import tarfile
from pathlib import Path

import structlog

from nexus.db.pg_provision import PgBinaries

_log = structlog.get_logger(__name__)

#: Env override pointing at an explicit ``.txz`` (set by the distribution launcher
#: or by tests). Highest priority in :func:`locate_bundle_archive`.
BUNDLE_ENV = "NEXUS_PG_BUNDLE"

#: Marker dropped at the extract root once a complete bundle is materialised, so a
#: re-run is a cheap no-op rather than a re-extract.
_EXTRACT_MARKER = ".nx_bundle_extracted"

#: Relocation provenance marker the build (``scripts/build_pg_bundle.sh``) places
#: at ``bundle/.build_prefix``. Its presence proves the archive is a genuine
#: relocatable nexus PG bundle rather than an arbitrary tarball.
_BUILD_PREFIX_MARKER = ".build_prefix"

#: Sub-directory of the config dir the bundle is extracted into.
_CACHE_SUBDIR = "pg-bundle"


def current_platform_tag() -> str:
    """Return the artifact platform tag for this host (matches ``nexus-pg-<tag>``).

    Release N targets: ``mac-arm64`` (darwin; mac-x64 is out of scope, owner call),
    ``linux-amd64`` (x86_64), ``linux-arm64`` (aarch64). Windows is release N+1.
    """
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "darwin":
        # mac-x64 is out of scope for the bundle (owner call); return its true tag
        # anyway so locate simply finds no artifact and falls back to host PG,
        # rather than mislabelling an Intel Mac as mac-arm64.
        return "mac-arm64" if machine in {"arm64", "aarch64"} else "mac-x64"
    if system == "linux":
        return "linux-arm64" if machine in {"aarch64", "arm64"} else "linux-amd64"
    raise RuntimeError(
        f"No PostgreSQL bundle target for platform {system!r}/{machine!r} "
        "(Windows is a release N+1 follow-on)."
    )


def bundle_bin_dir(extract_root: Path) -> Path:
    """The ``bin/`` directory inside an extracted bundle tree."""
    return extract_root / "bundle" / "bin"


def _build_prefix_marker(extract_root: Path) -> Path:
    """The ``bundle/.build_prefix`` relocation-provenance marker."""
    return extract_root / "bundle" / _BUILD_PREFIX_MARKER


def is_bundle_extracted(extract_root: Path) -> bool:
    """True when *extract_root* holds a COMPLETE, marked bundle.

    Requires both the extraction marker AND all four required binaries, so a
    half-written tree (interrupted extract) is treated as not-extracted and is
    re-extracted rather than silently used.
    """
    marker = extract_root / _EXTRACT_MARKER
    return marker.is_file() and PgBinaries.from_dir(bundle_bin_dir(extract_root)).all_present()


def _default_search_dirs() -> list[Path]:
    """Conventional locations for the ship-alongside ``.txz`` (best-effort).

    The distribution launcher should set ``NEXUS_PG_BUNDLE`` explicitly; this is
    the fallback for an archive sitting next to the running executable.
    """
    return [Path(sys.executable).resolve().parent]


def locate_bundle_archive(*, search_dirs: list[Path] | None = None) -> Path | None:
    """Find the PG bundle ``.txz`` for this platform.

    Resolution order:

    1. ``NEXUS_PG_BUNDLE`` — an explicit archive path. Set-but-missing is a loud
       error (a misconfigured override must never silently fall through to a
       different bundle or to host PG).
    2. ``nexus-pg-<platform-tag>.txz`` in *search_dirs* (default: the directory of
       the running executable).

    Returns ``None`` when no bundle is found — the caller then falls back to host
    PostgreSQL discovery (dev boxes, host-installed PG).
    """
    env = os.environ.get(BUNDLE_ENV, "").strip()
    if env:
        archive = Path(env)
        if not archive.is_file():
            raise FileNotFoundError(
                f"{BUNDLE_ENV} is set to '{env}' but no such file exists. "
                f"Fix or unset {BUNDLE_ENV}."
            )
        return archive

    name = f"nexus-pg-{current_platform_tag()}.txz"
    for d in (search_dirs if search_dirs is not None else _default_search_dirs()):
        candidate = Path(d) / name
        if candidate.is_file():
            return candidate
    return None


def _archive_identity(archive: Path) -> str:
    """Cheap identity of *archive* for the extraction marker (nexus-xzop6).

    Name plus size plus mtime, not a digest: this runs on the provisioning
    path and a bundle is hundreds of megabytes, so hashing it on every call
    would be a real cost to detect a case the download already gated. The
    sha256 that matters is verified at download time by
    ``binary_install`` and recorded in ``nexus-pg.meta.json``; this only has
    to notice that the archive on disk is no longer the one that produced
    the extracted tree.
    """
    stat = archive.stat()
    return f"source={archive}\nname={archive.name}\nsize={stat.st_size}\nmtime_ns={stat.st_mtime_ns}\n"


def extract_bundle(archive: Path, extract_root: Path) -> Path:
    """Idempotently extract *archive* to *extract_root*; return the ``bin/`` dir.

    A complete prior extraction of THE SAME archive is a no-op — the existing
    tree is preserved, so a user-modified or already-provisioned bundle is never
    clobbered on re-run. A bad archive (incomplete tree) raises ``RuntimeError``
    and leaves no marker.

    Being handed a DIFFERENT archive re-extracts (nexus-xzop6). The marker
    used to record ``source=`` and nothing ever read it, so any complete prior
    tree satisfied the check and a new bundle was silently ignored — the old
    binaries stayed and ``pg_bundle_already_extracted`` was logged. That was
    latent only because ``PG_VERSION`` has been 17.5 for the whole shipped
    history; the first bump would have left every existing install running the
    old PostgreSQL with no signal.

    NOTE the half this does NOT solve: a data directory initialised by PG N
    cannot be started by PG N+1 binaries. Swapping the binaries under an
    existing cluster surfaces as a loud PostgreSQL startup refusal, not
    silent corruption, but a real major bump additionally needs pg_upgrade or
    dump/restore. That is RDR-scale and deliberately out of scope here.
    """
    bin_dir = bundle_bin_dir(extract_root)
    marker = extract_root / _EXTRACT_MARKER
    identity = _archive_identity(archive)
    if is_bundle_extracted(extract_root):
        recorded = marker.read_text() if marker.is_file() else ""
        if recorded == identity:
            _log.debug("pg_bundle_already_extracted", extract_root=str(extract_root))
            return bin_dir
        # A pre-xzop6 marker carries only "source=..." and cannot be compared;
        # treat it as a mismatch and re-extract once to adopt the new format.
        _log.warning(
            "pg_bundle_archive_changed_reextracting",
            extract_root=str(extract_root),
            archive=str(archive),
            had_identity=bool(recorded and "size=" in recorded),
            note=(
                "extracting a different PG bundle over an existing tree; if this "
                "is a major-version change, an existing data directory will need "
                "pg_upgrade or dump/restore before the new binaries can start it"
            ),
        )
        shutil.rmtree(extract_root, ignore_errors=True)

    extract_root.mkdir(parents=True, exist_ok=True)
    _log.info("pg_bundle_extracting", archive=str(archive), extract_root=str(extract_root))
    try:
        with tarfile.open(archive, "r:xz") as tf:
            # filter="data" (3.12+) blocks path traversal / unsafe members.
            tf.extractall(extract_root, filter="data")
    except Exception:
        # Corrupt archive / disk-full / permission error mid-extract leaves a
        # partial tree. Remove it so a retry starts clean and a half-written tree
        # is never mistaken for a usable bundle (no marker is written).
        shutil.rmtree(extract_root, ignore_errors=True)
        raise

    if not PgBinaries.from_dir(bin_dir).all_present():
        present = [p.name for p in bin_dir.glob("*")] if bin_dir.is_dir() else []
        shutil.rmtree(extract_root, ignore_errors=True)
        raise RuntimeError(
            f"PG bundle '{archive}' extracted an incomplete tree: "
            f"missing one or more of initdb/pg_ctl/psql/createdb under "
            f"{bin_dir} (found: {present or 'none'})."
        )
    if not _build_prefix_marker(extract_root).is_file():
        shutil.rmtree(extract_root, ignore_errors=True)
        raise RuntimeError(
            f"PG bundle '{archive}' is missing its '{_BUILD_PREFIX_MARKER}' "
            "relocation marker — not a genuine nexus PG bundle "
            "(scripts/build_pg_bundle.sh stamps it at bundle/.build_prefix)."
        )

    # Atomic marker: write to a temp file then rename, so a kill mid-write never
    # leaves a truncated marker that a re-run would trust.
    tmp = extract_root / (_EXTRACT_MARKER + ".tmp")
    tmp.write_text(identity)
    tmp.replace(marker)
    _log.info("pg_bundle_extracted", bin_dir=str(bin_dir))
    return bin_dir


def extracted_bin_dir(config_dir: Path) -> Path | None:
    """Return the bundle ``bin/`` dir if a COMPLETE bundle is already extracted.

    Cheap, archive-free check of the stable cache location
    (``<config_dir>/pg-bundle``). Used by
    :func:`nexus.db.pg_provision.discover_pg_binaries` so EVERY consumer of PG
    binaries (init, the storage-service daemon's PG-restart path, ``_psql_bin``)
    finds the bundle on a local-distribution machine — not only the one-shot
    ``nx init`` process that performed the extraction. Returns ``None`` when no
    extracted bundle is present (host-PG / dev mode).
    """
    extract_root = config_dir / _CACHE_SUBDIR
    return bundle_bin_dir(extract_root) if is_bundle_extracted(extract_root) else None


def ensure_pg_bundle(
    config_dir: Path, *, search_dirs: list[Path] | None = None
) -> Path | None:
    """Locate + extract the ship-alongside PG bundle for first-run provisioning.

    Returns the extracted ``bin/`` directory (to be exported as ``NEXUS_PG_BIN``),
    or ``None`` when no bundle is present — in which case the caller proceeds with
    host PostgreSQL discovery (dev / host-installed PG). Idempotent: a second call
    after a successful extract is a cheap no-op.
    """
    archive = locate_bundle_archive(search_dirs=search_dirs)
    if archive is None:
        return None
    return extract_bundle(archive, config_dir / _CACHE_SUBDIR)
