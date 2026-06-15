# SPDX-License-Identifier: AGPL-3.0-or-later
"""CA-3 live test: relocatable zonky PG16 + injected pgvector (RDR-157 P1, bead nexus-vwvv5.2).

RDR-157 Critical Assumption 3 (CA-3): the *assemble* strategy (Strategy A,
RF-157-7) works end to end — a relocatable zonky PG16 binary bundle, with a
CI-built ``vector.so`` injected into its ``pkglibdir``/``sharedir``, can be
driven by ``nx``'s own provisioner to a running cluster that loads pgvector.

This is the gate before the P2/P3 build-out. If it FAILS, RDR-157 falls back to
Strategy B (build PostgreSQL from source in the native-image CI matrix).

How the bundle is materialized
------------------------------
These tests do not download or build anything themselves — they are pure
*verification* over a pre-materialized bundle so they stay deterministic and
hermetic. The CI job ``ca3-pgvector-bundle`` (``.github/workflows/ci.yml``)
performs the side-effecting work:

  1. fetch ``io.zonky.test.postgres:embedded-postgres-binaries-linux-amd64:16.x``
     from Maven Central and extract its inner ``postgres-linux-x86_64.txz``,
  2. build ``pgvector`` against the extracted tree's ``pg_config`` inside a
     **manylinux2014 (glibc 2.17)** container so the ``.so``'s glibc floor
     matches zonky's broad-compat baseline rather than the runner's glibc,
  3. ``make install`` the extension into the extracted tree,
  4. export ``NEXUS_CA3_BUNDLE=<extracted-root>`` and run this module with
     ``-m integration``.

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
# manylinux2014 == glibc 2.17 (CentOS 7 baseline). The CI job builds vector.so
# in that image precisely so this floor holds. linux-aarch64's live run is
# deferred to the P2/P3 build matrix (needs an arm64 runner); its floor is the
# same manylinux2014_aarch64 baseline (glibc 2.17) and is asserted here as the
# documented target value, exercised live when the aarch64 bundle job lands.
GLIBC_FLOOR: tuple[int, int] = (2, 17)


# ── Bundle discovery / skip gate ───────────────────────────────────────────────

def _bundle_root() -> Path | None:
    """The extracted zonky bundle root, or None when not materialized.

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
    pytest.mark.skipif(
        _BUNDLE is None,
        reason=(
            "skipped: no CA-3 zonky bundle — set NEXUS_CA3_BUNDLE to an extracted "
            "PG16 tree with pgvector injected (provided by the ci.yml "
            "'ca3-pgvector-bundle' job)"
        ),
    ),
]


# ── Helpers ────────────────────────────────────────────────────────────────────

def _bundle_bins() -> PgBinaries:
    """Resolve the bundle's binaries via the production NEXUS_PG_BIN seam."""
    assert _BUNDLE is not None  # guarded by pytestmark
    os.environ["NEXUS_PG_BIN"] = str(_BUNDLE / "bin")
    return discover_pg_binaries()


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


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def bins() -> PgBinaries:
    return _bundle_bins()


@pytest.fixture(scope="module")
def provisioned(bins: PgBinaries, tmp_path_factory):
    """Provision a hermetic cluster *from the bundle* (NEXUS_PG_BIN points at it).

    Mirrors the production path: discover_pg_binaries honours NEXUS_PG_BIN, so
    provision() builds the cluster with the relocatable bundle binaries.
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


# ── Test 1: zonky bundle ships a complete, relocatable PG16 ─────────────────────

class TestZonkyBundle:
    def test_binaries_discovered_via_env_seam(self, bins):
        assert bins.all_present(), "zonky bundle missing one of initdb/pg_ctl/psql/createdb"

    def test_reports_postgres_16(self, bins):
        out = subprocess.run(
            [str(bins.bin_dir / "initdb"), "--version"],
            capture_output=True, text=True, check=True,
        ).stdout
        assert "16." in out, f"expected PostgreSQL 16.x, got: {out.strip()!r}"

    def test_sharedir_resolves_inside_bundle(self, bins):
        """Relocatable proof: pg_config --sharedir is UNDER the extracted root,
        not a hard-coded system path baked at zonky's build time."""
        sharedir = Path(_pg_config(bins, "--sharedir")).resolve()
        assert _BUNDLE is not None
        root = _BUNDLE.resolve()
        assert root in sharedir.parents or sharedir == root, (
            f"sharedir {sharedir} is not inside the relocated bundle {root} — "
            "binaries are not relocatable"
        )


# ── Test 2: pgvector injected and visible to the preflight ──────────────────────

class TestPgvectorInjected:
    def test_control_file_present(self, bins):
        sharedir = Path(_pg_config(bins, "--sharedir"))
        control = sharedir / "extension" / "vector.control"
        assert control.is_file(), f"vector.control not injected at {control}"

    def test_shared_object_present(self, bins):
        pkglibdir = Path(_pg_config(bins, "--pkglibdir"))
        assert (pkglibdir / "vector.so").is_file(), (
            f"vector.so not injected into {pkglibdir}"
        )

    def test_preflight_passes(self, bins):
        """check_pgvector_available must NOT raise once the control file is in place."""
        try:
            check_pgvector_available(bins)
        except PgVectorNotInstalledError as exc:  # pragma: no cover - failure path
            pytest.fail(f"preflight wrongly reported pgvector missing: {exc}")


# ── Test 3: glibc floor pinned (RF-157-9 regression guard) ──────────────────────

class TestGlibcFloor:
    def test_vector_so_within_pinned_floor(self, bins):
        """vector.so must not require a glibc newer than the pinned per-target
        floor. A builder that silently raises the requirement (e.g. building on
        ubuntu-latest instead of manylinux2014) fails HERE, not at a user's
        dlopen on an older distro."""
        pkglibdir = Path(_pg_config(bins, "--pkglibdir"))
        required = _max_glibc_requirement(pkglibdir / "vector.so")
        assert required <= GLIBC_FLOOR, (
            f"vector.so requires GLIBC_{required[0]}.{required[1]} > pinned floor "
            f"GLIBC_{GLIBC_FLOOR[0]}.{GLIBC_FLOOR[1]}; build pgvector on "
            "manylinux2014 (glibc 2.17) to match zonky's baseline (RF-157-9)"
        )

    def test_postgres_binary_within_pinned_floor(self, bins):
        """The zonky server binary's own glibc floor must also hold — the
        effective install floor is max(postgres, vector.so)."""
        postgres = bins.bin_dir / "postgres"
        required = _max_glibc_requirement(postgres)
        assert required <= GLIBC_FLOOR, (
            f"zonky postgres binary requires GLIBC_{required[0]}.{required[1]} > "
            f"pinned floor GLIBC_{GLIBC_FLOOR[0]}.{GLIBC_FLOOR[1]} — zonky's "
            "linux-amd64 baseline drifted above the documented target (RF-157-9)"
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
            capture_output=True, text=True,
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
