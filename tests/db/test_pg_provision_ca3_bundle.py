# SPDX-License-Identifier: AGPL-3.0-or-later
"""CA-3 live test: complete PG17 tree + pgvector (RDR-157 P1, bead nexus-vwvv5.2).

RDR-157 Critical Assumption 3 (CA-3): the *assemble* strategy works end to end —
a complete PG17 binary tree, with a CI-built ``vector.so`` in its
``pkglibdir``/``sharedir``, can be driven by ``nx``'s own provisioner to a
running cluster that loads pgvector, with the binaries' glibc floor pinned.

This is the gate before the P2/P3 build-out.

Strategy B, not zonky. CA-3 empirically falsified the original "zonky reduced
bundle + inject pgvector" plan (RF-157-7): zonky's
``embedded-postgres-binaries`` reduced bundle ships ONLY initdb/pg_ctl/postgres
— no ``pg_config``/``psql``/``createdb``/headers — so pgvector cannot be built
against it AND ``discover_pg_binaries`` (which requires psql+createdb) cannot
use it. The RDR pre-authorized **Strategy B** (build PostgreSQL from source) as
the CA-3-failure fallback; this gate proves that path.

How the bundle is materialized
------------------------------
These tests do not build anything themselves — they are pure *verification* over
a pre-materialized bundle so they stay deterministic and hermetic. The CI job
``ca3-pgvector-bundle`` (``.github/workflows/ci.yml``) performs the
side-effecting work inside a **manylinux_2_28 (glibc 2.28)** container:

  1. ``./configure --prefix=<bundle> && make && make install`` PostgreSQL 17
     from source — a complete tree (initdb, pg_ctl, postgres, psql, createdb,
     pg_config, headers, pgxs),
  2. build ``pgvector`` against that ``pg_config`` and ``make install`` it,
  3. export ``NEXUS_CA3_BUNDLE=<bundle>`` and run this module with
     ``-m integration``.

The bundle is built at, and mounted at, the same path it is consumed from, so
``pg_config``'s compiled-in paths agree with the runtime location. True
extract-to-arbitrary-dir relocation (where ``pg_config``'s build-prefix-boundness
becomes a real concern) is a P3 bundle-build problem (``nexus-vwvv5.9``), not
this gate.

Locally (e.g. on a darwin dev box) the bundle is absent, so every test skips
with a reason that names the providing CI job. The CI job additionally asserts
the core test actually *ran* (see the workflow), so a silent all-skip can never
masquerade as a pass.

Glibc floor (RF-157-9)
----------------------
``check_pgvector_available`` only stats ``vector.control`` — it never dlopens
``vector.so``. The real ABI/glibc failure surfaces at ``CREATE EXTENSION
vector`` (extension LOAD time), which is why :class:`TestCreateExtensionLive`
is the load-bearing assertion. :class:`TestGlibcFloor` pins the maximum
``GLIBC_x.y`` symbol version the ``.so`` may require, so a future builder
upgrade that silently raises the floor fails the test instead of shipping a
binary that ``dlopen``-fails on older distros.
"""
from __future__ import annotations

import os
import re
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

# ── Pinned glibc floor per linux target (RF-157-9) ─────────────────────────────
#
# manylinux_2_28 == glibc 2.28 (AlmaLinux 8 baseline: RHEL8 / Debian 10 /
# Ubuntu 18.10+). The CI job builds vector.so in that image precisely so this
# floor holds. (manylinux2014 / glibc 2.17 was rejected: CentOS 7 is EOL with
# dead in-container repos.) Both linux targets run this module live in CI
# (linux-amd64 + linux-arm64, via the ci.yml matrix); the floor is the same
# manylinux_2_28 baseline (glibc 2.28) on both. mac-arm64 needs a darwin variant
# (dyld/otool minos, not GLIBC) — nexus-0ixqc.
GLIBC_FLOOR: tuple[int, int] = (2, 28)

# Per-OS shared-object name + floor mechanism. On macOS pgvector builds
# ``vector.dylib`` and the loader is ``dyld`` (no GLIBC); the analog of the
# glibc floor is the Mach-O ``LC_BUILD_VERSION`` minos — the binary won't load
# on a macOS older than its deployment target. The macOS CI job pins
# MACOSX_DEPLOYMENT_TARGET to this value (nexus-0ixqc).
_IS_DARWIN = sys.platform == "darwin"
_VECTOR_LIB = "vector.dylib" if _IS_DARWIN else "vector.so"
MACOS_MIN_FLOOR: tuple[int, int] = (13, 0)  # macOS 13 Ventura — a defensible 2026 floor


# ── Bundle discovery / skip gate ───────────────────────────────────────────────

def _bundle_root() -> Path | None:
    """The PG17 bundle root, or None when not materialized.

    Set by the CI ``ca3-pgvector-bundle`` job. Absent locally → tests skip.
    """
    raw = os.environ.get("NEXUS_CA3_BUNDLE", "").strip()
    if not raw:
        return None
    root = Path(raw)
    return root if (root / "bin" / "initdb").is_file() else None


_BUNDLE = _bundle_root()

pytestmark = [
    pytest.mark.integration,
    # CA-3 drives PostgreSQL directly via psql; it never launches the JVM
    # service jar, so exempt it from tests/db/conftest.py's jar-freshness gate.
    pytest.mark.no_service_jar,
    pytest.mark.skipif(
        _BUNDLE is None,
        reason=(
            "skipped: no CA-3 bundle — set NEXUS_CA3_BUNDLE to a complete PG17 "
            "tree with pgvector (provided by the ci.yml "
            "'ca3-pgvector-bundle' job)"
        ),
    ),
]


# ── Helpers ────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def bundle_bin_env():
    """Point NEXUS_PG_BIN at the bundle for the module, restore on teardown.

    The restore matters: without it the bundle path leaks into the process
    environment and a later pg_provision test in the same pytest invocation
    would resolve the bundle binaries instead of the system PostgreSQL.
    """
    assert _BUNDLE is not None  # guarded by pytestmark
    old = os.environ.get("NEXUS_PG_BIN")
    os.environ["NEXUS_PG_BIN"] = str(_BUNDLE / "bin")
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("NEXUS_PG_BIN", None)
        else:
            os.environ["NEXUS_PG_BIN"] = old


def _pg_config(bins: PgBinaries, flag: str) -> str:
    out = subprocess.run(
        [str(bins.bin_dir / "pg_config"), flag],
        capture_output=True, text=True, check=True, timeout=10,
    )
    return out.stdout.strip()


def _max_glibc_requirement(so_path: Path) -> tuple[int, int]:
    """Highest GLIBC_x.y symbol version *required* by an ELF shared object.

    Parses ``objdump -T`` version-reference records. Returns (0, 0) when the
    object references no versioned glibc symbols.
    """
    out = subprocess.run(
        ["objdump", "-T", str(so_path)],
        capture_output=True, text=True, check=True,
    )
    versions: list[tuple[int, int]] = []
    for m in re.finditer(r"GLIBC_(\d+)\.(\d+)", out.stdout):
        versions.append((int(m.group(1)), int(m.group(2))))
    return max(versions) if versions else (0, 0)


def _macos_minos(macho_path: Path) -> tuple[int, int]:
    """macOS deployment-target (LC_BUILD_VERSION minos) of a Mach-O object.

    Parses ``otool -l`` for the ``minos X.Y`` line. Returns (0, 0) when no
    LC_BUILD_VERSION/LC_VERSION_MIN load command is present.
    """
    out = subprocess.run(
        ["otool", "-l", str(macho_path)],
        capture_output=True, text=True, check=True,
    )
    versions: list[tuple[int, int]] = []
    for m in re.finditer(r"minos (\d+)\.(\d+)", out.stdout):
        versions.append((int(m.group(1)), int(m.group(2))))
    # The binary's effective floor is the HIGHEST minos across its load commands.
    return max(versions) if versions else (0, 0)


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def bins(bundle_bin_env) -> PgBinaries:
    """Resolve the bundle's binaries via the production NEXUS_PG_BIN seam."""
    return discover_pg_binaries()


@pytest.fixture(scope="module")
def provisioned(bins: PgBinaries, tmp_path_factory):
    """Provision a hermetic cluster *from the bundle* (NEXUS_PG_BIN points at it).

    Mirrors the production path: discover_pg_binaries honours NEXUS_PG_BIN, so
    provision() builds the cluster with the bundle binaries.
    """
    config_dir = tmp_path_factory.mktemp("nexus_ca3_bundle")
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


# ── Test 1: the bundle ships a complete PG17 with consistent paths ──────────────

class TestCompletePg17Bundle:
    def test_binaries_discovered_via_env_seam(self, bins):
        assert bins.all_present(), "bundle missing one of initdb/pg_ctl/psql/createdb"

    def test_reports_postgres_17(self, bins):
        out = subprocess.run(
            [str(bins.bin_dir / "initdb"), "--version"],
            capture_output=True, text=True, check=True,
        ).stdout
        assert "17." in out, f"expected PostgreSQL 17.x, got: {out.strip()!r}"

    def test_sharedir_resolves_inside_bundle(self, bins):
        """pg_config --sharedir resolves UNDER the bundle root (the bundle is
        built at, and consumed from, the same prefix)."""
        sharedir = Path(_pg_config(bins, "--sharedir")).resolve()
        assert _BUNDLE is not None
        root = _BUNDLE.resolve()
        assert root in sharedir.parents or sharedir == root, (
            f"sharedir {sharedir} is not inside the bundle {root} — "
            "pg_config paths disagree with the bundle location"
        )


# ── Test 2: pgvector injected and visible to the preflight ──────────────────────

class TestPgvectorInjected:
    def test_control_file_present(self, bins):
        sharedir = Path(_pg_config(bins, "--sharedir"))
        control = sharedir / "extension" / "vector.control"
        assert control.is_file(), f"vector.control not injected at {control}"

    def test_shared_object_present(self, bins):
        pkglibdir = Path(_pg_config(bins, "--pkglibdir"))
        assert (pkglibdir / _VECTOR_LIB).is_file(), (
            f"{_VECTOR_LIB} not injected into {pkglibdir}"
        )

    def test_pg_trgm_present(self, bins):
        """pg_trgm (contrib, required by the RDR-155 schema) must be in the
        bundle the CA-3 gate validates — not only in the relocation smoke. The
        build (build_pg_bundle.sh) installs it; a regression dropping it would
        otherwise pass this canonical gate green. Mirrors
        test_pg_bundle_relocation.py::test_pg_trgm_present_under_root."""
        sharedir = Path(_pg_config(bins, "--sharedir"))
        control = sharedir / "extension" / "pg_trgm.control"
        assert control.is_file(), (
            f"pg_trgm.control not present at {control} — contrib extension "
            "missing from the bundle (RDR-155 schema requires it)"
        )

    def test_preflight_passes(self, bins):
        """check_pgvector_available must NOT raise once the control file is in place."""
        try:
            check_pgvector_available(bins)
        except PgVectorNotInstalledError as exc:  # pragma: no cover - failure path
            pytest.fail(f"preflight wrongly reported pgvector missing: {exc}")


# ── Test 3: glibc floor pinned (RF-157-9 regression guard; linux only) ──────────

@pytest.mark.skipif(_IS_DARWIN, reason="GLIBC floor is linux-only; macOS uses TestMacosFloor")
class TestGlibcFloor:
    def test_vector_so_within_pinned_floor(self, bins):
        """vector.so must not require a glibc newer than the pinned per-target
        floor. A builder that silently raises the requirement (e.g. building on
        ubuntu-latest instead of manylinux_2_28) fails HERE, not at a user's
        dlopen on an older distro."""
        pkglibdir = Path(_pg_config(bins, "--pkglibdir"))
        required = _max_glibc_requirement(pkglibdir / "vector.so")
        # Non-vacuity guard: a real pgvector .so always references versioned
        # glibc symbols. (0, 0) means objdump found none — wrong file, empty
        # output, or static-libc linkage — which would let the floor check
        # pass without proving anything.
        assert required > (0, 0), (
            f"no GLIBC_x.y symbols found in {pkglibdir / 'vector.so'} — objdump "
            "produced no versioned references; the floor check would be vacuous"
        )
        assert required <= GLIBC_FLOOR, (
            f"vector.so requires GLIBC_{required[0]}.{required[1]} > pinned floor "
            f"GLIBC_{GLIBC_FLOOR[0]}.{GLIBC_FLOOR[1]}; build pgvector on "
            "manylinux_2_28 (glibc 2.28) to match a broad-compat baseline (RF-157-9)"
        )

    def test_postgres_binary_within_pinned_floor(self, bins):
        """The server binary's own *direct* glibc floor must also hold — the
        effective install floor is max(postgres, vector.so).

        Note: ``objdump -T`` reports only the symbols the ELF references
        directly, not those reached transitively through shared libraries it
        links. The from-source build deliberately drops icu/zlib/readline/ssl
        (see the CI configure flags), so postgres' transitive surface is
        essentially libc, making the direct check representative here."""
        postgres = bins.bin_dir / "postgres"
        required = _max_glibc_requirement(postgres)
        # Same non-vacuity guard as vector.so: a dynamically-linked postgres
        # always references versioned glibc symbols; (0, 0) means objdump saw
        # none (wrong path / static build) and the floor check would be vacuous.
        assert required > (0, 0), (
            f"no GLIBC_x.y symbols found in {postgres} — objdump produced no "
            "versioned references; the floor check would be vacuous"
        )
        assert required <= GLIBC_FLOOR, (
            f"postgres binary requires GLIBC_{required[0]}.{required[1]} > "
            f"pinned floor GLIBC_{GLIBC_FLOOR[0]}.{GLIBC_FLOOR[1]} — the build "
            "baseline drifted above the documented target (RF-157-9)"
        )


# ── Test 3 (macOS): deployment-target floor pinned (dyld analog of the glibc floor) ─

@pytest.mark.skipif(not _IS_DARWIN, reason="macOS minos floor; linux uses TestGlibcFloor")
class TestMacosFloor:
    def test_vector_dylib_within_minos_floor(self, bins):
        """vector.dylib must not require a macOS newer than the pinned deployment
        target. A build that silently raises minos (e.g. forgetting
        MACOSX_DEPLOYMENT_TARGET, so it targets the runner's OS) fails HERE, not
        at a user's dyld load on an older macOS."""
        pkglibdir = Path(_pg_config(bins, "--pkglibdir"))
        minos = _macos_minos(pkglibdir / _VECTOR_LIB)
        assert minos > (0, 0), (
            f"no LC_BUILD_VERSION minos in {pkglibdir / _VECTOR_LIB} — otool found "
            "no deployment target; the floor check would be vacuous"
        )
        assert minos <= MACOS_MIN_FLOOR, (
            f"vector.dylib requires macOS {minos[0]}.{minos[1]} > pinned floor "
            f"{MACOS_MIN_FLOOR[0]}.{MACOS_MIN_FLOOR[1]}; set "
            f"MACOSX_DEPLOYMENT_TARGET={MACOS_MIN_FLOOR[0]}.{MACOS_MIN_FLOOR[1]} "
            "when building (nexus-0ixqc)"
        )

    def test_postgres_binary_within_minos_floor(self, bins):
        """The server binary's own deployment-target floor must also hold — the
        effective install floor is max(postgres, vector.dylib)."""
        postgres = bins.bin_dir / "postgres"
        minos = _macos_minos(postgres)
        assert minos > (0, 0), (
            f"no LC_BUILD_VERSION minos in {postgres} — otool found no deployment "
            "target; the floor check would be vacuous"
        )
        assert minos <= MACOS_MIN_FLOOR, (
            f"postgres binary requires macOS {minos[0]}.{minos[1]} > pinned floor "
            f"{MACOS_MIN_FLOOR[0]}.{MACOS_MIN_FLOOR[1]} — the build's "
            "MACOSX_DEPLOYMENT_TARGET drifted above the documented target (nexus-0ixqc)"
        )


# ── Test 4: CREATE EXTENSION vector live (the CA-3 go/no-go) ─────────────────────

class TestCreateExtensionLive:
    """The load-bearing assertion: provision a cluster from the bundle and
    actually LOAD pgvector. This forces dlopen(vector.so) — the failure mode the
    glibc floor and Strategy A exist to defend against."""

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
            f"bundle cluster not accepting connections on {result.port}"
        )

    def test_create_extension_vector_loads(self, provisioned, bins):
        result, _ = provisioned
        proc = self._query(bins, result.port, "CREATE EXTENSION IF NOT EXISTS vector")
        assert proc.returncode == 0, (
            "CREATE EXTENSION vector failed — pgvector did not load from the "
            f"bundle (dlopen/ABI failure):\n{proc.stderr}"
        )

    def test_vector_type_usable(self, provisioned, bins):
        result, _ = provisioned
        self._query(bins, result.port, "CREATE EXTENSION IF NOT EXISTS vector")
        proc = self._query(
            bins, result.port,
            "SELECT '[1,2,3]'::vector <-> '[3,2,1]'::vector",
        )
        assert proc.returncode == 0 and proc.stdout.strip(), (
            f"vector distance op failed after load:\n{proc.stderr}"
        )
