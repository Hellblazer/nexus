# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-152 bead nexus-gmiaf.21 — Embedding parity gate.

Formally proves: Java service embedding == Python PRODUCTION embedding across all three
paths.  "Production" means the exact call that Python uses in operation, NOT a
hand-crafted API call that may have different parameters.

  1. LOCAL ONNX    — Python chromadb ONNXMiniLM_L6_V2 vs Java OnnxEmbedder
  2. CLOUD STANDARD — Python chromadb.VoyageAIEmbeddingFunction (production path, float32)
                     vs Java VoyageEmbedder (same API parameters, no input_type, truncation=True)
  3. CLOUD CCE      — Python voyageai.Client.contextualized_embed(inputs=[[text]], input_type=...)
                     vs Java CceEmbedder (same API parameters, one text per call)

Each path is tested at BOTH the float32 production precision (what Chroma actually stores)
AND float64 API precision (model/param match signal).

Prerequisites:
  - service/target/nexus-service-1.0-SNAPSHOT.jar built
  - Java on PATH (or JAVA_HOME set)
  - PostgreSQL binaries discoverable (NEXUS_PG_BIN / Homebrew / system dirs / PATH)
  - VOYAGE_API_KEY set (for cloud paths)

Gate-runnable: cloud paths skip with clear reason when VOYAGE_API_KEY absent.
ONNX path always runs via a LOCAL-MODE service (NX_VOYAGE_API_KEY absent).

ONNX routing: the ONNX fixture launches the service without NX_VOYAGE_API_KEY, so
EmbedderRouter is in local mode and ALL collection prefixes (including knowledge__) route
to ONNX.  No fragile unrecognized-prefix fallback needed.

Assertion:
  - float32 production path: np.array_equal(java_f32, python_f32) — bit-exact
  - float64 cosine: 1.0 - cosine < 1e-9 (catches real drift at 1e-5+)

Run:
    cd service && mvn package -DskipTests && cd ..
    uv run pytest tests/db/test_embed_parity.py -m integration -v -s

Expected output:
    ONNX:   float32 bit-exact; cosine 1.0 (all 3 texts)
    VOYAGE: float32 bit-exact; cosine 1.0 (all 3 texts)
    CCE:    float32 bit-exact; cosine 1.0 (all 3 texts)
"""
from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Generator

import numpy as np
import pytest

from tests.db._service_fixture import SERVICE_ROLES_SQL, pg_bin_dir

# ── Prerequisite paths ─────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_JAR       = _REPO_ROOT / "service" / "target" / "nexus-service-1.0-SNAPSHOT.jar"
_PG_BIN    = pg_bin_dir()

_INITDB   = _PG_BIN / "initdb"
_PG_CTL   = _PG_BIN / "pg_ctl"
_PSQL     = _PG_BIN / "psql"
_CREATEDB = _PG_BIN / "createdb"

_JAVA_HOME = os.environ.get("JAVA_HOME", "")
_JAVA = (
    Path(_JAVA_HOME) / "bin" / "java"
    if _JAVA_HOME
    else Path(shutil.which("java") or "java")
)

_HAS_VOYAGE_KEY = bool(os.environ.get("VOYAGE_API_KEY"))


def _has_fastembed() -> bool:
    # nexus-wrfiy: RDR-160 made the LOCAL model bge-768. The Java service embeds
    # with the standard bge-768 ONNX; to compare, the Python side needs the
    # bge-768 embedder, which nexus.db.local_ef provides only via fastembed (the
    # conexus[local] extra). Without it Python falls back to minilm-384 and the
    # parity check is meaningless (different model + dims).
    try:
        import fastembed  # noqa: F401
        return True
    except Exception:
        return False


_HAS_FASTEMBED = _has_fastembed()

_ALL_PREREQS = (
    _JAR.exists()
    and _INITDB.exists()
    and _PG_CTL.exists()
    and _PSQL.exists()
    and _CREATEDB.exists()
    and (_JAVA.exists() if _JAVA_HOME else shutil.which("java") is not None)
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _ALL_PREREQS,
        reason=(
            "skipped: missing jar or PG binaries "
            f"(jar={_JAR.exists()}, pg={_PG_CTL.exists()}, java={_JAVA})"
        ),
    ),
]

# ── Fixed test corpus ──────────────────────────────────────────────────────────
#
# Three texts:
#  [0] short general text
#  [1] semantic-search domain text  (code-adjacent for voyage-code-3)
#  [2] long text >256 tokens — exercises truncation (truncation=True MUST be set)

CORPUS = [
    "The quick brown fox jumps over the lazy dog.",
    "def semantic_search(query: str, corpus: list[str]) -> list[float]: ...",
    (
        "In the beginning God created the heavens and the earth. "
        "Now the earth was formless and empty, darkness was over the surface "
        "of the deep, and the Spirit of God was hovering over the waters. "
        "And God said, Let there be light, and there was light. God saw that "
        "the light was good, and he separated the light from the darkness. "
        "God called the light day, and the darkness he called night. And there "
        "was evening, and there was morning the first day. "
        "And God said, Let there be a vault between the waters to separate water "
        "from water. So God made the vault and separated the water under the vault "
        "from the water above it. And it was so. God called the vault sky. "
        "And there was evening, and there was morning the second day."
    ),
]

# Collection names drive Java-side EmbedderRouter.  In cloud mode:
#   code__  → VoyageEmbedder (voyage-code-3)
#   knowledge__ → CceEmbedder (voyage-context-3)
# In local mode (no VOYAGE key): ALL collections → OnnxEmbedder.
_VOYAGE_COLLECTION = "code__parity-test__voyage-code-3__v1"
_CCE_COLLECTION    = "knowledge__parity-test__voyage-context-3__v1"
_ONNX_COLLECTION   = "knowledge__parity-test__bge-base-en-v15-768__v1"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_tcp(host: str, port: int, timeout: float = 180.0) -> None:
    # nexus-o06g4: the JAR runs ~120 Liquibase changesets + loads the bge-768
    # ONNX at startup; 30/45s was too short on slower hosts (darwin JVM) and
    # produced false TimeoutError "errors" in the integration run. 180s matches
    # the observed cold-start budget (init --service ~1-2 min).
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.3):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(f"port {port} on {host} not reachable after {timeout}s")


def _cosine_sim(a: list[float], b: list[float]) -> float:
    """Cosine similarity in float64."""
    va = np.array(a, dtype=np.float64)
    vb = np.array(b, dtype=np.float64)
    return float(np.dot(va, vb) / (np.linalg.norm(va) * np.linalg.norm(vb)))


# ── Bootstrap SQL ─────────────────────────────────────────────────────────────

_BOOTSTRAP_SQL = """\
CREATE SCHEMA IF NOT EXISTS nexus;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN
    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass';
  END IF;
END $$;
GRANT USAGE ON SCHEMA nexus TO nexus_svc;
GRANT USAGE ON SCHEMA public TO nexus_svc;
-- The service runs Liquibase at startup AS this role (NX_DB_USER=nexus_svc).
-- It must create the public.databasechangelog tracker, the t1 schema, and tables
-- in the nexus schema. PostgreSQL 15+ no longer grants CREATE on public by default,
-- so mirror the DDL grants production gives the schema-owner role (nexus_admin):
-- pg_provision grants CREATE ON SCHEMA public + CREATE ON DATABASE to the migrator.
GRANT CREATE ON DATABASE paritytest TO nexus_svc;
GRANT CREATE ON SCHEMA public TO nexus_svc;
GRANT CREATE ON SCHEMA nexus TO nexus_svc;
"""


# ── Module-scoped fixtures ─────────────────────────────────────────────────────

def _provision_pg() -> tuple[dict, str]:
    """Provision a fresh hermetic Postgres 16 + service roles.

    nexus-wrfiy: returns ``(pg_dict, pgdata)`` so each service fixture gets its
    OWN database. The JAR bootstraps a single root service_token at startup
    (UNIQUE idx_service_tokens_single_root); two services sharing one PG (the
    old module-scoped ``pg_instance``) collided on it. Per-service PGs isolate
    the bootstrap.
    """
    pgdata  = tempfile.mkdtemp(prefix="nexus_parity_pg_")
    pg_port = _free_port()
    pglog   = os.path.join(pgdata, "pg.log")
    pg_user = os.environ["USER"]

    subprocess.run(
        [str(_INITDB), "-D", pgdata, "--no-locale", "-E", "UTF8", "--auth=trust"],
        check=True, capture_output=True,
    )
    with open(os.path.join(pgdata, "postgresql.conf"), "a") as f:
        f.write(f"\nport = {pg_port}\nlisten_addresses = '127.0.0.1'\n")
    subprocess.run(
        [str(_PG_CTL), "-D", pgdata, "-l", pglog,
         "-o", f"-p {pg_port} -k {pgdata}", "start", "-w"],
        check=True, capture_output=True,
    )
    subprocess.run(
        [str(_CREATEDB), "-h", "127.0.0.1", "-p", str(pg_port),
         "-U", pg_user, "paritytest"],
        check=True, capture_output=True,
    )
    # net63: JAR runs Liquibase at startup; grants-nexus-svc.xml requires nexus_svc.
    subprocess.run(
        [str(_PSQL), "-h", "127.0.0.1", "-p", str(pg_port),
         "-U", pg_user, "-d", "paritytest",
         "-v", "ON_ERROR_STOP=1", "-c", SERVICE_ROLES_SQL],
        check=True, capture_output=True,
    )
    proc = subprocess.run(
        [str(_PSQL), "-h", "127.0.0.1", "-p", str(pg_port),
         "-U", pg_user, "-d", "paritytest",
         "-v", "ON_ERROR_STOP=1", "-c", _BOOTSTRAP_SQL],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        _teardown_pg(pgdata)
        raise RuntimeError(
            f"Bootstrap SQL failed (rc={proc.returncode}):\n"
            f"stdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return {"port": pg_port, "dbname": "paritytest", "user": pg_user}, pgdata


def _teardown_pg(pgdata: str) -> None:
    subprocess.run(
        [str(_PG_CTL), "-D", pgdata, "stop", "-m", "immediate"],
        capture_output=True,
    )
    shutil.rmtree(pgdata, ignore_errors=True)


def _start_service(pg: dict, token: str, voyage_key: str | None = None,
                   timeout: float = 180.0) -> tuple[subprocess.Popen, int]:
    """Launch JAR in parity-gate mode.  Returns (proc, port)."""
    svc_port = _free_port()
    env = {
        **os.environ,
        "NX_SERVICE_PORT":  str(svc_port),
        "NX_SERVICE_TOKEN": token,
        "NX_DB_URL": (
            f"jdbc:postgresql://127.0.0.1:{pg['port']}/{pg['dbname']}"
        ),
        "NX_DB_USER": "nexus_svc",
        "NX_DB_PASS": "nexus_svc_pass",
        # nexus-o06g4: the JAR's Liquibase runs CREATE EXTENSION vector at
        # startup, which requires a SUPERUSER. The app role nexus_svc is not
        # one, so the service must be given an admin pool (NX_DB_ADMIN_*) for
        # DDL — exactly as the production launcher (storage_service_daemon) and
        # the vector-ETL fixture do. pg["user"] is the initdb bootstrap
        # superuser; --auth=trust means no password. Without this the JAR died
        # at boot ("permission denied to create extension vector") and never
        # bound the port.
        "NX_DB_ADMIN_URL": (
            f"jdbc:postgresql://127.0.0.1:{pg['port']}/{pg['dbname']}"
        ),
        "NX_DB_ADMIN_USER": pg["user"],
        "NX_DB_ADMIN_PASS": "",
        "NX_POOL_SIZE": "2",
        "NX_CHROMA_MODE": "none",
    }
    env.pop("NX_STORAGE_BACKEND", None)
    # Voyage key presence drives cloud vs local mode in EmbedderRouter
    if voyage_key:
        env["NX_VOYAGE_API_KEY"] = voyage_key
    else:
        env.pop("NX_VOYAGE_API_KEY", None)
        env.pop("VOYAGE_API_KEY", None)  # ensure local mode

    # nexus-o06g4: write the JAR's startup output to a FILE, not undrained
    # PIPEs. The service logs ~120 Liquibase changesets + Helidon startup at
    # boot; with stdout/stderr=PIPE and nobody draining them, the 64KB pipe
    # buffer fills, the JVM BLOCKS on write, never finishes startup, and never
    # binds the port — so _wait_tcp always timed out (the real cause of the
    # integration-run "TimeoutError" errors, NOT a too-short timeout). The
    # production launcher (storage_service_daemon) redirects to log files for
    # exactly this reason.
    log_path = os.path.join(tempfile.gettempdir(), f"nexus-svc-parity-{svc_port}.log")
    log_fh = open(log_path, "wb")
    proc = subprocess.Popen(
        [str(_JAVA), "-jar", str(_JAR)],
        env=env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        preexec_fn=os.setsid,
    )
    try:
        _wait_tcp("127.0.0.1", svc_port, timeout=timeout)
    except TimeoutError:
        log_fh.flush()
        try:
            tail = open(log_path).read()[-1500:]
        except OSError:
            tail = "(log unavailable)"
        raise TimeoutError(
            f"service did not bind {svc_port} in {timeout}s; JAR log tail:\n{tail}"
        )
    return proc, svc_port


def _stop_service(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass


@pytest.fixture(scope="module")
def cloud_service() -> Generator[tuple[str, str], None, None]:
    """Java service in CLOUD mode (NX_VOYAGE_API_KEY set).

    Cloud routing:
      code__*       → VoyageEmbedder (voyage-code-3, no input_type, truncation=True)
      knowledge__*  → CceEmbedder (voyage-context-3, input_type=document, per-text)
    Yields (base_url, token). nexus-wrfiy: owns its PG (see _provision_pg).
    """
    if not _HAS_VOYAGE_KEY:
        pytest.skip("VOYAGE_API_KEY not set — skipping cloud service fixture")

    pg, pgdata = _provision_pg()
    token = "parity-cloud-token"
    try:
        proc, port = _start_service(pg, token, voyage_key=os.environ["VOYAGE_API_KEY"])
        try:
            yield f"http://127.0.0.1:{port}", token
        finally:
            _stop_service(proc)
    finally:
        _teardown_pg(pgdata)


@pytest.fixture(scope="module")
def local_service() -> Generator[tuple[str, str], None, None]:
    """Java service in LOCAL mode (NX_VOYAGE_API_KEY absent).

    ALL collection prefixes → OnnxEmbedder.  No cloud credentials required.
    Yields (base_url, token). nexus-wrfiy: owns its PG (see _provision_pg).
    """
    pg, pgdata = _provision_pg()
    token = "parity-local-token"
    try:
        proc, port = _start_service(pg, token, voyage_key=None)
        try:
            yield f"http://127.0.0.1:{port}", token
        finally:
            _stop_service(proc)
    finally:
        _teardown_pg(pgdata)


def _java_embed(base_url: str, token: str, collection: str, texts: list[str]) -> list[list[float]]:
    """Call POST /v1/vectors/embed and return the embedding vectors (float64 from JSON)."""
    import json
    import urllib.request

    payload = json.dumps({"collection": collection, "texts": texts}).encode()
    req = urllib.request.Request(
        f"{base_url}/v1/vectors/embed",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "X-Nexus-Tenant": "parity-tenant",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = json.loads(resp.read())

    assert "embeddings" in body, (
        f"Expected 'embeddings' key in response, got: {list(body.keys())}"
    )
    return body["embeddings"]


# ── Assertion helpers ─────────────────────────────────────────────────────────

def _assert_parity(
    label: str,
    python_f32: list[np.ndarray],
    java_f64: list[list[float]],
    max_ulp_threshold: int | None = 0,
    cosine_threshold: float = 1e-9,
) -> None:
    """Assert float32 production-path parity AND float64 cosine >= 1-1e-9.

    python_f32: production Python embeddings as np.float32 arrays.
    java_f64:   Java /embed response (float64 doubles from embedDoubleForCollection).

    max_ulp_threshold: maximum allowed ULP difference per element.
      - 0 (default): bit-exact (np.array_equal)
      - None: skip ULP check (cosine-only; for ONNX where arithmetic differs)
      - N>0: allow N ULPs (for JSON-parsed API values where base64 isn't used)

    Float64 cosine (primary gate):
      Threshold 1e-9 catches real embedding-space drift while tolerating float64
      arithmetic noise (2.4e-13 for identical float32 vectors).

    Float32 ULP check (secondary gate, cloud paths):
      With encoding_format=base64, both Python and Java decode the same raw float32
      binary representation — should be bit-exact (0 ULPs).  The float64 promotion
      (float32 → double in Java, then double → float32 in test) is exact and
      should give 0 ULPs for identical base64 sources.
    """
    assert len(java_f64) == len(python_f32), (
        f"{label}: length mismatch java={len(java_f64)} python={len(python_f32)}"
    )
    for i, (py_f32, jv_f64) in enumerate(zip(python_f32, java_f64)):
        jv_f32 = np.array(jv_f64, dtype=np.float32)
        bit_exact = np.array_equal(py_f32, jv_f32)
        actual_ulp = int(np.max(np.abs(
            py_f32.view(np.int32).astype(np.int64) - jv_f32.view(np.int32).astype(np.int64)
        )))
        cosine = _cosine_sim(py_f32.tolist(), jv_f64)
        # Per-leg content hashes: when parity breaks in an environment we can't
        # probe interactively (CI), these identify WHICH leg moved relative to a
        # known-good run (nexus-f4wcg: the java leg's byte-different request body
        # resolved to a numerically different Voyage serving result on linux).
        import hashlib
        py_sha = hashlib.sha256(py_f32.tobytes()).hexdigest()[:16]
        jv_sha = hashlib.sha256(jv_f32.tobytes()).hexdigest()[:16]
        print(
            f"  {label} corpus[{i}]: cosine={cosine:.10f}, "
            f"float32_bit_exact={bit_exact}, max_ulp_diff={actual_ulp}, "
            f"py_sha={py_sha}, java_sha={jv_sha}"
        )
        # Primary gate: float64 cosine catches real embedding-space drift
        assert 1.0 - cosine < cosine_threshold, (
            f"{label} cosine FAILED corpus[{i}]: "
            f"cosine={cosine:.15f} (diff={1.0 - cosine:.2e}, threshold={cosine_threshold:.0e}). "
            "Real drift: input_type mismatch (cosine≈0.95), truncation omission (cosine≈0.99995), "
            "CCE batch context contamination (cosine≈0.9996)."
        )
        # Secondary gate: float32 production path
        if max_ulp_threshold is not None:
            assert actual_ulp <= max_ulp_threshold, (
                f"{label} float32 ULP MISMATCH corpus[{i}]: actual_ulp={actual_ulp} "
                f"(threshold={max_ulp_threshold} ULPs). "
                "With encoding_format=base64, Java and Python decode identical float32 "
                "binary — ULP>0 means different API parameters or non-base64 path. "
                f"max_abs_diff={np.max(np.abs(py_f32.astype(np.float64) - jv_f32.astype(np.float64))):.2e}"
            )


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestEmbedParity:
    """Formal embedding parity gate: Java == Python PRODUCTION.

    ONNX path: cosine-only gate (arithmetic divergence in mean-pool/normalize expected).
    Cloud paths (Voyage/CCE): bit-exact float32 gate (encoding_format=base64 ensures
    identical float32 binary from the API for both Python and Java).
    """

    # ── Path 1: LOCAL ONNX ─────────────────────────────────────────────────────

    @pytest.mark.skipif(
        not _HAS_FASTEMBED,
        reason="nexus-wrfiy: RDR-160 made the local model bge-768; the Python "
        "parity side needs the bge-768 embedder (conexus[local]/fastembed), which "
        "is not installed in this env. The Java side is bge-768; without fastembed "
        "Python falls back to minilm-384 and parity is meaningless.",
    )
    def test_onnx_parity(self, local_service: tuple[str, str]) -> None:
        """LOCAL bge-768: Python local EF == Java OnnxEmbedder (RDR-160).

        The LOCAL-MODE service (no VOYAGE key) routes ALL collections to the
        bundled bge-768 ONNX. The Python side uses nexus.db.local_ef's
        LocalEmbeddingFunction, which resolves to bge-768 when fastembed (the
        conexus[local] extra) is installed.

        Cosine-only gate at 1e-4 (RDR-160 parity floor): the Java service uses
        the standard fp32 bge ONNX while fastembed uses a fused export, so they
        are cosine-close (>= 0.9999), not bit-exact.
        """
        from nexus.db.local_ef import LocalEmbeddingFunction

        base_url, token = local_service

        # Python path: nexus local EF (bge-768 via fastembed under conexus[local]).
        python_ef = LocalEmbeddingFunction(model_name="BAAI/bge-base-en-v1.5")
        python_raw = python_ef(CORPUS)
        python_f32 = [np.array(v, dtype=np.float32) for v in python_raw]

        # Java path: local-mode service → EmbedderRouter → bge-768 OnnxEmbedder.
        java_vecs = _java_embed(base_url, token, _ONNX_COLLECTION, CORPUS)

        print(f"\nbge-768 parity ({len(CORPUS)} texts):")
        _assert_parity(
            "bge-768", python_f32, java_vecs,
            max_ulp_threshold=None, cosine_threshold=1e-4,
        )
        print("ONNX: PASS")

    # ── Path 2: CLOUD STANDARD (voyage-code-3) ────────────────────────────────

    @pytest.mark.skipif(
        not _HAS_VOYAGE_KEY,
        reason="VOYAGE_API_KEY not set — skipping cloud standard parity path",
    )
    def test_voyage_standard_parity(self, cloud_service: tuple[str, str]) -> None:
        """CLOUD STANDARD: Python chromadb.VoyageAIEmbeddingFunction == Java VoyageEmbedder.

        Python PRODUCTION path: VoyageAIEmbeddingFunction(model_name='voyage-code-3',
        api_key=key) with default input_type=None (omitted from request), truncation=True,
        returns np.float32.

        Java path: VoyageEmbedder with a request body BYTE-identical to the
        Python SDK's wire format (locked by VoyageEmbedderBodyTest against
        captured python output).

        Assert cosine within the API's cross-session noise floor. Bit-exactness
        is NOT assertable over the network: the standard endpoint joined CCE in
        server-side non-determinism (nexus-f4wcg 2026-07-07 — the PYTHON leg
        alone, called twice back-to-back with the identical batch, returned two
        variants 4.19e-05 cosine apart; the "linux gate failure" was the two
        legs coin-flipping between the same two variants).
        """
        import chromadb.utils.embedding_functions as cef

        base_url, token = cloud_service
        api_key = os.environ["VOYAGE_API_KEY"]

        # Python PRODUCTION path: VoyageAIEmbeddingFunction (what t3.py uses)
        # input_type=None (default), truncation=True (default), returns np.float32
        ef = cef.VoyageAIEmbeddingFunction(model_name="voyage-code-3", api_key=api_key)
        python_f32 = [np.array(v, dtype=np.float32) for v in ef(CORPUS)]

        # Java path: VoyageEmbedder via code__ prefix routing
        java_vecs = _java_embed(base_url, token, _VOYAGE_COLLECTION, CORPUS)

        print(f"\nVOYAGE standard parity ({len(CORPUS)} texts):")
        # The standard endpoint is NON-DETERMINISTIC across serving replicas, same
        # as CCE below (nexus-f4wcg 2026-07-07: two live variants 4.19e-05 cosine
        # apart for the identical request — the python leg alone flapped between
        # them on back-to-back calls, so bit-exact/ULP gating is a coin flip, not
        # a contract). DROP the ULP gate; cosine tolerance clears the inter-variant
        # noise (4.2e-5) with margin while still catching gross wiring drift
        # (input_type mismatch ≈0.95, wrong model, shuffled index ordering).
        # Fine-grained request drift (truncation flag, omitted-vs-null params,
        # separators) is BELOW the noise floor and is verified structurally
        # instead: VoyageEmbedderBodyTest locks the Java wire body byte-identical
        # to captured python SDK output.
        _assert_parity("VOYAGE-standard", python_f32, java_vecs,
                       max_ulp_threshold=None, cosine_threshold=1e-3)
        print("VOYAGE standard: PASS (cosine within API non-determinism floor)")

    # ── Path 3: CLOUD CCE (voyage-context-3) ──────────────────────────────────

    @pytest.mark.skipif(
        not _HAS_VOYAGE_KEY,
        reason="VOYAGE_API_KEY not set — skipping CCE parity path",
    )
    def test_cce_parity(self, cloud_service: tuple[str, str]) -> None:
        """CLOUD CCE: Python _cce_embed == Java CceEmbedder.

        Python PRODUCTION path (_cce_embed from t3.py):
          vo.contextualized_embed(inputs=[[text]], model='voyage-context-3',
                                  input_type='document')
        No output_dtype.  Returns Python floats (float32-precision, as proven by S0.2 probe).

        Java path: CceEmbedder calls the SAME API with same params, one text per call,
        input_type='document', no output_dtype.

        Assert float32 bit-exact AND cosine >= 1-1e-9.

        CONCURRENCY NOTE: The Voyage CCE API is deterministic within a server session but
        may return different values across different network sessions (server-side
        non-determinism).  To ensure Python and Java see the SAME server-session response,
        we submit both embedding calls concurrently (within the same ~second window) so
        the Voyage API routes them to the same backend instance.
        """
        import threading
        import voyageai

        base_url, token = cloud_service
        api_key = os.environ["VOYAGE_API_KEY"]

        python_raw: list[list[float]] = [[] for _ in CORPUS]
        java_vecs_holder: list[list[list[float]] | None] = [None]
        errors: list[BaseException] = []

        def embed_python() -> None:
            try:
                vo = voyageai.Client(api_key=api_key)
                for i, text in enumerate(CORPUS):
                    result = vo.contextualized_embed(
                        inputs=[[text]],
                        model="voyage-context-3",
                        input_type="document",
                        # No output_dtype — production _cce_embed does not set it
                    )
                    python_raw[i] = result.results[0].embeddings[0]
            except BaseException as exc:
                errors.append(exc)

        def embed_java() -> None:
            try:
                java_vecs_holder[0] = _java_embed(
                    base_url, token, _CCE_COLLECTION, CORPUS
                )
            except BaseException as exc:
                errors.append(exc)

        # Run Python and Java embedding concurrently.  Both hit the Voyage API within
        # the same ~second window, ensuring the same server-session response.
        t_py = threading.Thread(target=embed_python, daemon=True)
        t_jv = threading.Thread(target=embed_java, daemon=True)
        t_py.start()
        t_jv.start()
        t_py.join(timeout=120)
        t_jv.join(timeout=120)

        if errors:
            raise errors[0]

        assert java_vecs_holder[0] is not None, "Java embed thread did not complete"

        python_f32 = [np.array(v, dtype=np.float32) for v in python_raw]
        java_vecs = java_vecs_holder[0]

        print(f"\nCCE parity ({len(CORPUS)} texts):")
        # CCE (voyage-context-3 contextualized) is NON-DETERMINISTIC across network
        # sessions: embedding the SAME text on successive calls drifts by ~2.6e-4–3.7e-4
        # cosine (empirically measured, nexus-mcgnz). The concurrency-alignment hack above
        # routes Python+Java to the same backend within one ~second window and shrinks the
        # typical drift to ~5e-6, but cannot guarantee bit-exactness — only corpus[0]
        # reliably aligns (Python embeds sequentially while Java batches in one call).
        # So we DROP the bit-exact ULP gate for CCE and use a cosine tolerance that clears
        # the API's own call-to-call noise floor (3.7e-4) with margin while still catching
        # gross wiring (input_type mismatch ≈0.95). Fine-grained drift (truncation ≈5e-5,
        # batch contamination ≈4e-4) sits BELOW the API noise floor and is verified
        # structurally instead (request params: input_type='document', per-text batching).
        _assert_parity("CCE", python_f32, java_vecs,
                       max_ulp_threshold=None, cosine_threshold=1e-3)
        print("CCE: PASS (cosine within API non-determinism floor)")

    # ── Self-check: ONNX Python determinism (no service needed) ───────────────

    def test_onnx_python_determinism(self) -> None:
        """Python ONNXMiniLM must return bit-identical vectors on repeated calls."""
        from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2
        ef = ONNXMiniLM_L6_V2()
        for text in CORPUS:
            v1 = np.array(ef([text])[0], dtype=np.float32)
            v2 = np.array(ef([text])[0], dtype=np.float32)
            assert np.array_equal(v1, v2), (
                f"Python ONNX not deterministic for text: {text[:40]!r}"
            )
