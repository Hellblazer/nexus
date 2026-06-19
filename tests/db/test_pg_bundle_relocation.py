# SPDX-License-Identifier: AGPL-3.0-or-later
"""P3.1 relocation smoke: a packaged PG17+pgvector bundle, EXTRACTED to an
arbitrary directory distinct from its build ``--prefix``, still provisions and
loads pgvector (RDR-157 P3.1, bead nexus-vwvv5.10).

Why this is distinct from the CA-3 gate
---------------------------------------
``tests/db/test_pg_provision_ca3_bundle.py`` proves the *assemble* path with the
bundle consumed AT its build prefix (prefix == mount path), so ``pg_config``'s
compiled-in paths happen to agree with the runtime location. CA-3 explicitly
**deferred true relocation** — extract-to-arbitrary-dir, where ``pg_config``'s
build-prefix-boundness becomes a real concern — to P3.

A shippable local-distribution bundle is extracted to a user directory (e.g.
``~/.config/nexus/pg-bundle``) that is NOT the build ``--prefix``. PostgreSQL is
relocatable by design: its programs (including ``pg_config``) resolve
``share``/``lib`` relative to the executable's own location via
``find_my_exec`` — so after extraction ``pg_config --sharedir`` reports the NEW
location, not the build prefix. nexus's :func:`check_pgvector_available`
additionally re-anchors the sharedir on ``bin_dir``
(:func:`nexus.db.pg_provision._candidate_sharedirs`) as belt-and-suspenders.
This module proves the FUNCTIONAL outcome end to end against a genuinely
relocated tree:

  1. the extracted root is NOT the build ``--prefix`` (we really did relocate —
     non-vacuity), ``pg_config`` resolves its paths back inside that new root,
     and
  2. ``discover_pg_binaries`` + ``provision`` + ``CREATE EXTENSION vector`` all
     succeed from the new location.

How the relocated tree is materialized
--------------------------------------
The CI job builds the bundle from source at one prefix, packages it to a
``.txz``, then EXTRACTS that tarball to ``$RUNNER_TEMP/relocated`` and exports
``NEXUS_PG_BUNDLE_ROOT`` at the extracted root. ``scripts/build_pg_bundle.sh``
drops a ``.build_prefix`` marker recording the original configure ``--prefix``
so this module can assert root != build-prefix without guessing.

Locally (e.g. a darwin dev box) the bundle is absent, so every test skips with a
reason naming the providing job. The CI job additionally asserts the core
``CREATE EXTENSION`` test actually ran (``scripts/assert_ca3_ran.py``), so a
silent all-skip can never masquerade as a pass.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from nexus.db.pg_provision import (
    NEXUS_DB_NAME,
    PgBinaries,
    PgVectorNotInstalledError,
    ProvisionResult,
    _port_accepting,
    check_pgvector_available,
    discover_pg_binaries,
    provision,
)

_IS_DARWIN = sys.platform == "darwin"
_VECTOR_LIB = "vector.dylib" if _IS_DARWIN else "vector.so"

# Pinned floors for the PACKAGED artifact (must match the CA-3 gate's floors in
# tests/db/test_pg_provision_ca3_bundle.py). The relocation smoke re-checks them
# on the extracted libs because the .txz this step uploads is what P3.2/P3.4
# consume — a floor regression must fail at artifact time, not only at assemble
# time. The floor is a static property of the shared object (objdump/otool), so
# it holds even though the linux smoke runs on the host's newer glibc.
GLIBC_FLOOR: tuple[int, int] = (2, 28)        # manylinux_2_28 baseline (RF-157-9)
MACOS_MIN_FLOOR: tuple[int, int] = (13, 0)    # macOS 13 Ventura (nexus-0ixqc)

# Pinned PG version (the CI env sets PG_VERSION=17.5). Exact, not a "17." prefix
# (project rule: exact assertions in regression tests).
PG_VERSION_PIN = "17.5"

# Substring of the bundle-absence skip reason. assert_ca3_ran.py keys on
# "no CA-3 bundle"; keep this phrase aligned with that marker so the same
# non-vacuity asserter guards this relocation smoke too.
_SKIP_REASON = (
    "skipped: no CA-3 bundle (relocated) — set NEXUS_PG_BUNDLE_ROOT to a "
    "PG17+pgvector tree EXTRACTED from its .txz to a dir other than its build "
    "prefix (provided by the ci.yml 'ca3-pgvector-bundle' relocation step)"
)


def _relocated_root() -> Path | None:
    """The relocated (extracted) bundle root, or None when not materialized."""
    raw = os.environ.get("NEXUS_PG_BUNDLE_ROOT", "").strip()
    if not raw:
        return None
    root = Path(raw)
    return root if (root / "bin" / "initdb").is_file() else None


_ROOT = _relocated_root()

pytestmark = [
    pytest.mark.integration,
    # Drives PostgreSQL directly via psql; never launches the JVM service jar.
    pytest.mark.no_service_jar,
    pytest.mark.skipif(_ROOT is None, reason=_SKIP_REASON),
]


# ── Fixtures ────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def bundle_bin_env():
    """Point NEXUS_PG_BIN at the relocated bin/, restore on teardown.

    The restore matters: a leaked NEXUS_PG_BIN would make a later pg_provision
    test in the same invocation resolve the relocated bundle instead of the
    system PostgreSQL.
    """
    assert _ROOT is not None  # guarded by pytestmark
    old = os.environ.get("NEXUS_PG_BIN")
    os.environ["NEXUS_PG_BIN"] = str(_ROOT / "bin")
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("NEXUS_PG_BIN", None)
        else:
            os.environ["NEXUS_PG_BIN"] = old


@pytest.fixture(scope="module")
def bins(bundle_bin_env) -> PgBinaries:
    """Resolve the relocated binaries via the production NEXUS_PG_BIN seam."""
    return discover_pg_binaries()


@pytest.fixture(scope="module")
def provisioned(bins: PgBinaries, tmp_path_factory):
    """Provision a hermetic cluster from the RELOCATED tree."""
    config_dir = tmp_path_factory.mktemp("nexus_reloc_bundle")
    old_cfg = os.environ.get("NEXUS_CONFIG_DIR")
    os.environ["NEXUS_CONFIG_DIR"] = str(config_dir)
    try:
        result: ProvisionResult = provision(config_dir, force_new_port=True)
    finally:
        if old_cfg is None:
            os.environ.pop("NEXUS_CONFIG_DIR", None)
        else:
            os.environ["NEXUS_CONFIG_DIR"] = old_cfg

    yield result, config_dir

    pgdata = config_dir / "postgres"
    try:
        subprocess.run(
            [str(bins.bin_dir / "pg_ctl"), "-D", str(pgdata), "-m", "immediate", "stop"],
            capture_output=True, check=False, timeout=10,
        )
    except Exception:  # noqa: BLE001 — teardown must not reraise
        pass


def _pg_config(bins: PgBinaries, flag: str) -> str:
    out = subprocess.run(
        [str(bins.bin_dir / "pg_config"), flag],
        capture_output=True, text=True, check=True, timeout=10,
    )
    return out.stdout.strip()


def _reanchored(bins: PgBinaries, flag: str) -> Path:
    """A pg_config path re-anchored on the actual ``bin_dir``.

    Mirrors production :func:`nexus.db.pg_provision._candidate_sharedirs`: take
    the path's offset from ``pg_config --bindir`` and re-apply it to the resolved
    ``bin_dir``. Robust whether or not pg_config itself relocated.
    """
    reported = _pg_config(bins, flag)
    bindir = _pg_config(bins, "--bindir")
    rel = os.path.relpath(reported, bindir)
    return (bins.bin_dir / rel).resolve()


def _max_glibc_requirement(so_path: Path) -> tuple[int, int]:
    """Highest GLIBC_x.y symbol version *required* by an ELF shared object.

    Parses ``objdump -T`` version-reference records. Returns (0, 0) when the
    object references no versioned glibc symbols (the non-vacuity sentinel).
    """
    import re
    out = subprocess.run(
        ["objdump", "-T", str(so_path)], capture_output=True, text=True, check=True,
    )
    versions = [(int(a), int(b)) for a, b in re.findall(r"GLIBC_(\d+)\.(\d+)", out.stdout)]
    return max(versions) if versions else (0, 0)


def _macos_minos(macho_path: Path) -> tuple[int, int]:
    """macOS deployment-target (LC_BUILD_VERSION minos) of a Mach-O object.

    Returns (0, 0) when no minos load command is present (non-vacuity sentinel).
    """
    import re
    out = subprocess.run(
        ["otool", "-l", str(macho_path)], capture_output=True, text=True, check=True,
    )
    versions = [(int(a), int(b)) for a, b in re.findall(r"minos (\d+)\.(\d+)", out.stdout)]
    return max(versions) if versions else (0, 0)


# ── Test 1: we genuinely relocated (non-vacuity guard) ──────────────────────────

class TestActuallyRelocated:
    """Without these, a smoke that accidentally ran at the build prefix would
    pass without proving relocation — the same vacuity trap CA-3 guards against."""

    def test_root_differs_from_build_prefix(self):
        """The extracted root must NOT be the original configure --prefix.

        ``scripts/build_pg_bundle.sh`` records the build prefix in a
        ``.build_prefix`` marker at the bundle root; relocation extracted the
        tree somewhere else.
        """
        assert _ROOT is not None
        marker = _ROOT / ".build_prefix"
        assert marker.is_file(), (
            f"no .build_prefix marker at {marker} — build script must record the "
            "configure --prefix so relocation is provable"
        )
        build_prefix = Path(marker.read_text().strip()).resolve()
        assert _ROOT.resolve() != build_prefix, (
            f"relocated root {_ROOT.resolve()} equals the build prefix "
            f"{build_prefix} — the bundle was NOT relocated, smoke is vacuous"
        )

    def test_pg_config_resolves_inside_relocated_root(self, bins):
        """pg_config's reported sharedir must resolve back INSIDE the relocated
        root. PostgreSQL's relocatable-install support computes support-file
        paths relative to the executable (find_my_exec), so a tree extracted to
        a new prefix reports the new location, not the build prefix. This is the
        positive proof that the bundle's path resolution survived relocation."""
        assert _ROOT is not None
        sharedir = Path(_pg_config(bins, "--sharedir")).resolve()
        root = _ROOT.resolve()
        assert root == sharedir or root in sharedir.parents, (
            f"pg_config --sharedir {sharedir} resolved OUTSIDE the relocated root "
            f"{root} — PostgreSQL did not relocate its support-file paths; the "
            "tree layout was not preserved by the tarball"
        )


# ── Test 2: complete relocated tree + pgvector present ──────────────────────────

class TestRelocatedBundleComplete:
    def test_all_binaries_present(self, bins):
        assert bins.all_present(), (
            "relocated bundle missing one of initdb/pg_ctl/psql/createdb"
        )

    def test_reports_exact_pinned_postgres_version(self, bins):
        out = subprocess.run(
            [str(bins.bin_dir / "initdb"), "--version"],
            capture_output=True, text=True, check=True,
        ).stdout
        assert f"(PostgreSQL) {PG_VERSION_PIN}" in out, (
            f"expected PostgreSQL {PG_VERSION_PIN}, got: {out.strip()!r}"
        )

    def test_vector_lib_present_under_root(self, bins):
        """vector.so/.dylib must live UNDER the relocated root — the relative
        tree layout (bin/ lib/ share/) survived the tarball round-trip. Uses the
        bin-relative re-anchoring so it holds regardless of how pg_config reports
        pkglibdir."""
        pkglibdir = _reanchored(bins, "--pkglibdir")
        assert (pkglibdir / _VECTOR_LIB).is_file(), (
            f"{_VECTOR_LIB} not found under relocated root at {pkglibdir}"
        )

    def test_pg_trgm_present_under_root(self, bins):
        """pg_trgm (contrib, required by the RDR-155 schema) must also survive
        packaging + extraction — not just pgvector. The build verifies it
        pre-package; this re-verifies the extracted artifact."""
        sharedir = _reanchored(bins, "--sharedir")
        control = sharedir / "extension" / "pg_trgm.control"
        assert control.is_file(), (
            f"pg_trgm.control not found under relocated root at {control} — "
            "contrib extension lost in packaging/extraction"
        )

    def test_preflight_passes_after_relocation(self, bins):
        """check_pgvector_available must re-anchor and find vector.control under
        the relocated root despite pg_config's stale sharedir."""
        try:
            check_pgvector_available(bins)
        except PgVectorNotInstalledError as exc:  # pragma: no cover - failure path
            pytest.fail(
                f"preflight wrongly reported pgvector missing after relocation: {exc}"
            )


# ── Test 3: the PACKAGED artifact's compat floor is preserved ───────────────────

@pytest.mark.skipif(_IS_DARWIN, reason="GLIBC floor is linux-only; macOS uses TestPackagedMacosFloor")
class TestPackagedGlibcFloor:
    """The .txz uploaded by this step is what P3.2/P3.4 consume; re-assert the
    glibc floor on the EXTRACTED libs so an artifact-boundary regression fails
    here, not at a downstream user's dlopen on an older distro."""

    def test_vector_so_within_floor(self, bins):
        so = _reanchored(bins, "--pkglibdir") / "vector.so"
        required = _max_glibc_requirement(so)
        assert required > (0, 0), (
            f"no GLIBC_x.y symbols in {so} — objdump vacuous (wrong file/static link)"
        )
        assert required <= GLIBC_FLOOR, (
            f"vector.so requires GLIBC_{required[0]}.{required[1]} > floor "
            f"GLIBC_{GLIBC_FLOOR[0]}.{GLIBC_FLOOR[1]} (RF-157-9)"
        )

    def test_postgres_binary_within_floor(self, bins):
        required = _max_glibc_requirement(bins.bin_dir / "postgres")
        assert required > (0, 0), (
            f"no GLIBC_x.y symbols in {bins.bin_dir / 'postgres'} — objdump vacuous"
        )
        assert required <= GLIBC_FLOOR, (
            f"postgres requires GLIBC_{required[0]}.{required[1]} > floor "
            f"GLIBC_{GLIBC_FLOOR[0]}.{GLIBC_FLOOR[1]} (RF-157-9)"
        )


@pytest.mark.skipif(not _IS_DARWIN, reason="minos floor is macOS-only; linux uses TestPackagedGlibcFloor")
class TestPackagedMacosFloor:
    """Mach-O minos floor on the extracted dylib + server binary — the dyld
    analog of the glibc floor, re-asserted at artifact time."""

    def test_vector_dylib_within_floor(self, bins):
        dylib = _reanchored(bins, "--pkglibdir") / "vector.dylib"
        minos = _macos_minos(dylib)
        assert minos > (0, 0), (
            f"no LC_BUILD_VERSION minos in {dylib} — otool vacuous"
        )
        assert minos <= MACOS_MIN_FLOOR, (
            f"vector.dylib requires macOS {minos[0]}.{minos[1]} > floor "
            f"{MACOS_MIN_FLOOR[0]}.{MACOS_MIN_FLOOR[1]} (nexus-0ixqc)"
        )

    def test_postgres_binary_within_floor(self, bins):
        minos = _macos_minos(bins.bin_dir / "postgres")
        assert minos > (0, 0), (
            f"no LC_BUILD_VERSION minos in {bins.bin_dir / 'postgres'} — otool vacuous"
        )
        assert minos <= MACOS_MIN_FLOOR, (
            f"postgres requires macOS {minos[0]}.{minos[1]} > floor "
            f"{MACOS_MIN_FLOOR[0]}.{MACOS_MIN_FLOOR[1]} (nexus-0ixqc)"
        )


# ── Test 4: CREATE EXTENSION vector live from the relocated tree (go/no-go) ─────

class TestCreateExtensionFromRelocated:
    """The load-bearing assertion: provision a cluster from the RELOCATED bundle
    and actually LOAD pgvector. Forces dlopen of the shared object from the new
    location — the failure mode relocation could introduce."""

    def _query(self, bins, port, sql) -> subprocess.CompletedProcess:
        os_user = os.environ.get("USER") or os.environ.get("LOGNAME") or "postgres"
        return subprocess.run(
            [str(bins.bin_dir / "psql"), "-h", "127.0.0.1", "-p", str(port),
             "-U", os_user, "-d", NEXUS_DB_NAME, "-t", "-A", "-c", sql],
            capture_output=True, text=True, timeout=30,
        )

    def test_cluster_accepts_connections(self, provisioned):
        result, _ = provisioned
        assert _port_accepting("127.0.0.1", result.port), (
            f"relocated cluster not accepting connections on {result.port}"
        )

    def test_create_extension_vector_loads(self, provisioned, bins):
        result, _ = provisioned
        proc = self._query(bins, result.port, "CREATE EXTENSION IF NOT EXISTS vector")
        assert proc.returncode == 0, (
            "CREATE EXTENSION vector failed — pgvector did not load from the "
            f"RELOCATED bundle (dlopen/relocation failure):\n{proc.stderr}"
        )

    def test_vector_type_usable(self, provisioned, bins):
        result, _ = provisioned
        self._query(bins, result.port, "CREATE EXTENSION IF NOT EXISTS vector")
        proc = self._query(
            bins, result.port,
            "SELECT '[1,2,3]'::vector <-> '[3,2,1]'::vector",
        )
        assert proc.returncode == 0 and proc.stdout.strip(), (
            f"vector distance op failed after load from relocated tree:\n{proc.stderr}"
        )
