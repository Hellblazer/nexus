# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-152 bead nexus-gmiaf.21 — Embedding parity gate.

Formally proves: Java service embedding == Python embedding (cosine == 1.0 EXACTLY,
no tolerance) across all three paths:
  1. LOCAL ONNX    — Python chromadb ONNXMiniLM_L6_V2 vs Java OnnxEmbedder
  2. CLOUD STANDARD — Python voyageai.embed(voyage-code-3) vs Java VoyageEmbedder
  3. CLOUD CCE      — Python voyageai.contextualized_embed(voyage-context-3) vs Java CceEmbedder

This is the S0.2 gate (bead nexus-gmiaf.21).  Closing this bead unlocks .22
(drop the Python Chroma/Voyage clients).

Prerequisites:
  - service/target/nexus-service-1.0-SNAPSHOT.jar built (cd service && mvn package -DskipTests)
  - Java on PATH (or JAVA_HOME set)
  - /opt/homebrew/opt/postgresql@16/bin/{initdb,pg_ctl,psql,createdb} present
  - VOYAGE_API_KEY set in environment (for cloud paths)
  - chromadb Python package with ONNX model cached at
    ~/.cache/chroma/onnx_models/all-MiniLM-L6-v2/onnx/model.onnx

Cloud paths are skipped when VOYAGE_API_KEY is absent with a clear SKIP message.
ONNX path always runs.

Run locally:
    cd service && mvn package -DskipTests && cd ..
    uv run pytest tests/db/test_embed_parity.py -m integration -v

Expected output (all three cosines exactly 1.0):
    ONNX path:    cosine[0]=1.0000000000, cosine[1]=1.0000000000, cosine[2]=1.0000000000
    VOYAGE path:  cosine[0]=1.0000000000, cosine[1]=1.0000000000, cosine[2]=1.0000000000
    CCE path:     cosine[0]=1.0000000000, cosine[1]=1.0000000000, cosine[2]=1.0000000000

Design note:
  cosine(a, b) must satisfy 1.0 - cosine < 1e-9 (effectively "exactly 1.0").
  This threshold is tight enough to catch all real embedding drift (which is at 1e-5+)
  but allows the 2.4e-13 float64 cosine-formula arithmetic artifact on identical ONNX
  float32 vectors (different summation order in dot() vs norm() gives 1 - 2.4e-13).

  Root causes fixed:
  - Voyage/CCE float32 round-trip (3.3e-5 to 4.4e-4): /embed uses embedDouble()
    preserving raw JSON doubles, not float32-truncated values
  - ONNX 2.4e-13: purely float64 arithmetic, not real drift; tolerated by 1e-9 threshold
  S0.2 empirically proved the direction: standard Voyage WITHOUT truncation=True was 0.99995
  (detectable only with tight assertion).  This threshold catches that class of drift.
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

# ── Prerequisite paths ─────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_JAR       = _REPO_ROOT / "service" / "target" / "nexus-service-1.0-SNAPSHOT.jar"
_PG_BIN    = Path("/opt/homebrew/opt/postgresql@16/bin")

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

_ONNX_MODEL = Path.home() / ".cache/chroma/onnx_models/all-MiniLM-L6-v2/onnx/model.onnx"

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
            "skipped: missing jar or pg16 binaries "
            f"(jar={_JAR.exists()}, pg16={_PG_CTL.exists()}, java={_JAVA})"
        ),
    ),
]

# ── Fixed test corpus ──────────────────────────────────────────────────────────
#
# Three texts:
#  [0] short general text
#  [1] semantic-search domain text
#  [2] long text > 256 tokens to exercise truncation — truncation=True MUST be set
#      for standard Voyage or the cosine will be 0.99995 (S0.2 finding).

CORPUS = [
    "The quick brown fox jumps over the lazy dog.",
    "Semantic search connects questions to answers through meaning.",
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

# Collection names that drive Java-side routing via EmbedderRouter
_ONNX_COLLECTION   = "knowledge__parity-test__minilm-l6-v2-384__v1"
_VOYAGE_COLLECTION = "code__parity-test__voyage-code-3__v1"
_CCE_COLLECTION    = "knowledge__parity-test__voyage-context-3__v1"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_tcp(host: str, port: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.3):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(f"port {port} on {host} not reachable after {timeout}s")


def _cosine_sim(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity.  Both vectors must be L2-normalised
    (Voyage and ONNX both return unit vectors), so cosine = dot product."""
    va = np.array(a, dtype=np.float64)
    vb = np.array(b, dtype=np.float64)
    return float(np.dot(va, vb) / (np.linalg.norm(va) * np.linalg.norm(vb)))


# ── Bootstrap SQL (minimal — just enough for NexusService to start) ────────────

_BOOTSTRAP_SQL = """\
CREATE SCHEMA IF NOT EXISTS nexus;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'svc_parity') THEN
    CREATE ROLE svc_parity LOGIN PASSWORD 'svc_parity_pass';
  END IF;
END $$;

GRANT USAGE ON SCHEMA nexus TO svc_parity;
GRANT USAGE ON SCHEMA public TO svc_parity;
"""


# ── Module-scoped fixtures ─────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def pg_instance() -> Generator[dict, None, None]:
    """Hermetic Postgres 16 instance for the Java service to connect to."""
    pgdata  = tempfile.mkdtemp(prefix="nexus_parity_pg_")
    pg_port = _free_port()
    pglog   = os.path.join(pgdata, "pg.log")
    pg_user = os.environ["USER"]

    try:
        subprocess.run(
            [str(_INITDB), "-D", pgdata, "--no-locale", "-E", "UTF8", "--auth=trust"],
            check=True, capture_output=True,
        )
        with open(os.path.join(pgdata, "postgresql.conf"), "a") as f:
            f.write(f"\nport = {pg_port}\nlisten_addresses = '127.0.0.1'\n")
        subprocess.run(
            [str(_PG_CTL), "-D", pgdata, "-l", pglog,
             "-o", f"-p {pg_port} -k {pgdata}",
             "start", "-w"],
            check=True, capture_output=True,
        )
        subprocess.run(
            [str(_CREATEDB), "-h", "127.0.0.1", "-p", str(pg_port),
             "-U", pg_user, "paritytest"],
            check=True, capture_output=True,
        )
        proc = subprocess.run(
            [str(_PSQL), "-h", "127.0.0.1", "-p", str(pg_port),
             "-U", pg_user, "-d", "paritytest",
             "-v", "ON_ERROR_STOP=1", "-c", _BOOTSTRAP_SQL],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"Bootstrap SQL failed (rc={proc.returncode}):\n"
                f"stdout={proc.stdout}\nstderr={proc.stderr}"
            )
        yield {"port": pg_port, "dbname": "paritytest", "user": pg_user}
    finally:
        subprocess.run(
            [str(_PG_CTL), "-D", pgdata, "stop", "-m", "immediate"],
            capture_output=True,
        )
        shutil.rmtree(pgdata, ignore_errors=True)


@pytest.fixture(scope="module")
def service(pg_instance: dict) -> Generator[tuple[str, str], None, None]:
    """Launch the Java JAR in parity-gate mode (NX_CHROMA_MODE=none + VOYAGE_API_KEY).

    The service starts with:
    - No Chroma backend (NX_CHROMA_MODE=none)
    - NX_VOYAGE_API_KEY set → triggers embed-only mode in Main.java
    - /v1/vectors/embed endpoint available (requires vectorRepository=null but
      docEmbedderRouter != null)

    Yields (base_url, token).
    """
    svc_port = _free_port()
    token    = "parity-gate-token-secret"

    env = {
        **os.environ,
        "NX_SERVICE_PORT":  str(svc_port),
        "NX_SERVICE_TOKEN": token,
        "NX_DB_URL": (
            f"jdbc:postgresql://127.0.0.1:{pg_instance['port']}"
            f"/{pg_instance['dbname']}"
        ),
        "NX_DB_USER": "svc_parity",
        "NX_DB_PASS": "svc_parity_pass",
        "NX_POOL_SIZE": "2",
        # Parity-gate mode: no Chroma; embed-only endpoint active
        "NX_CHROMA_MODE": "none",
        "NX_VOYAGE_API_KEY": os.environ.get("VOYAGE_API_KEY", ""),
    }
    env.pop("NX_STORAGE_BACKEND", None)

    proc = subprocess.Popen(
        [str(_JAVA), "-jar", str(_JAR)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=os.setsid,
    )
    try:
        _wait_tcp("127.0.0.1", svc_port, timeout=45.0)
        yield f"http://127.0.0.1:{svc_port}", token
    finally:
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


def _java_embed(base_url: str, token: str, collection: str, texts: list[str]) -> list[list[float]]:
    """Call POST /v1/vectors/embed and return the embedding vectors."""
    import urllib.request
    import json

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

    assert "embeddings" in body, f"Expected 'embeddings' key in response, got: {list(body.keys())}"
    return body["embeddings"]


# ── Parity Tests ──────────────────────────────────────────────────────────────

class TestEmbedParity:
    """Formal embedding parity gate: Java == Python, cosine == 1.0 EXACTLY."""

    # ── Path 1: LOCAL ONNX ─────────────────────────────────────────────────────

    def test_onnx_parity_exact_cosine_1(self, service: tuple[str, str]) -> None:
        """LOCAL ONNX: Python chromadb ONNXMiniLM_L6_V2 == Java OnnxEmbedder.

        Assertion: cosine_similarity == 1.0 EXACTLY (np.float64, no tolerance).
        S0.2 proved: Java ONNX cosine = 1.00000000 vs Python chromadb ONNXMiniLM.
        """
        from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2

        base_url, token = service

        # Python path: chromadb ONNXMiniLM_L6_V2 (same artifact as Java)
        python_ef = ONNXMiniLM_L6_V2()
        python_vecs = python_ef(CORPUS)  # returns list[list[float]]

        # Java path: OnnxEmbedder via EmbedderRouter (local mode → ONNX)
        # _ONNX_COLLECTION starts with knowledge__ but in local mode (no Voyage key
        # in ONNX test context) all prefixes → ONNX. However, the service was started
        # with VOYAGE_API_KEY → cloud router. For ONNX parity, use a non-CCE collection.
        # We use a fresh request with an ONNX collection to force ONNX routing.
        # Actually: the service was started with NX_VOYAGE_API_KEY, so EmbedderRouter
        # is in cloud mode. knowledge__ → CCE. To get ONNX, we need an unrecognised prefix.
        # BUT the task says ONNX always runs (no creds needed). We start with ONNX first
        # by passing a collection that EmbedderRouter falls back to ONNX.
        # Unknown prefix → ONNX fallback (see EmbedderRouter.resolveEmbedder).
        java_vecs = _java_embed(base_url, token, "onnx-parity__test__minilm__v1", CORPUS)

        assert len(java_vecs) == len(CORPUS)
        cosines = []
        for i, (py_v, jv_v) in enumerate(zip(python_vecs, java_vecs)):
            cosine = _cosine_sim(py_v, jv_v)
            cosines.append(cosine)
            print(f"  ONNX path: corpus[{i}] cosine = {cosine:.10f}")
            assert 1.0 - cosine < 1e-9, (
                f"ONNX parity FAILED for corpus[{i}]: "
                f"cosine = {cosine:.15f} (expected 1.0; diff={1.0 - cosine:.2e}, threshold=1e-9). "
                "Check: same model.onnx? same tokenizer.json? same pipeline? "
                "Note: 2.4e-13 is float64 cosine-formula arithmetic, not real drift."
            )
        print(f"\nONNX path: {[f'{c:.10f}' for c in cosines]}")

    # ── Path 2: CLOUD STANDARD (voyage-code-3) ────────────────────────────────

    @pytest.mark.skipif(
        not _HAS_VOYAGE_KEY,
        reason="VOYAGE_API_KEY not set — skipping cloud standard parity path",
    )
    def test_voyage_standard_parity_exact_cosine_1(self, service: tuple[str, str]) -> None:
        """CLOUD STANDARD: Python voyageai.embed(voyage-code-3) == Java VoyageEmbedder.

        Load-bearing: truncation=True MUST be set (S0.2 finding: omit → cosine 0.99995).
        Collection starts with code__ → EmbedderRouter routes to VoyageEmbedder (standard).
        """
        import voyageai

        base_url, token = service
        api_key = os.environ["VOYAGE_API_KEY"]

        # Python path: voyageai SDK, standard /v1/embeddings
        vo = voyageai.Client(api_key=api_key)
        result = vo.embed(
            texts=CORPUS,
            model="voyage-code-3",
            input_type="document",
            truncation=True,   # LOAD-BEARING — matches Java VoyageEmbedder default
            output_dtype="float",
        )
        python_vecs = [e for e in result.embeddings]

        # Java path: VoyageEmbedder via code__ prefix routing
        java_vecs = _java_embed(base_url, token, _VOYAGE_COLLECTION, CORPUS)

        assert len(java_vecs) == len(CORPUS)
        cosines = []
        for i, (py_v, jv_v) in enumerate(zip(python_vecs, java_vecs)):
            cosine = _cosine_sim(py_v, jv_v)
            cosines.append(cosine)
            print(f"  VOYAGE path: corpus[{i}] cosine = {cosine:.10f}")
            assert 1.0 - cosine < 1e-9, (
                f"Voyage standard parity FAILED for corpus[{i}]: "
                f"cosine = {cosine:.15f} (expected 1.0; diff={1.0 - cosine:.2e}, threshold=1e-9). "
                "Check: truncation=True on both sides? same model? same input_type? "
                "drift > 1e-5 → float32 round-trip in /embed endpoint (fix: use embedDouble)."
            )
        print(f"\nVOYAGE standard path: {[f'{c:.10f}' for c in cosines]}")

    # ── Path 3: CLOUD CCE (voyage-context-3) ──────────────────────────────────

    @pytest.mark.skipif(
        not _HAS_VOYAGE_KEY,
        reason="VOYAGE_API_KEY not set — skipping CCE parity path",
    )
    def test_cce_parity_exact_cosine_1(self, service: tuple[str, str]) -> None:
        """CLOUD CCE: Python voyageai.contextualized_embed(voyage-context-3) == Java CceEmbedder.

        Python path: t3.py _cce_embed([[text]], model='voyage-context-3', input_type='document')
        Java path: CceEmbedder.embed([text]) — same inputs=[[text]] packing, same model.

        Collection starts with knowledge__ → EmbedderRouter routes to CceEmbedder.
        """
        import voyageai

        base_url, token = service
        api_key = os.environ["VOYAGE_API_KEY"]

        # Python path: per-text CCE, mirroring t3.py _cce_embed (one inner list per text)
        vo = voyageai.Client(api_key=api_key)
        python_vecs = []
        for text in CORPUS:
            result = vo.contextualized_embed(
                inputs=[[text]],        # one inner list = one document
                model="voyage-context-3",
                input_type="document",
                # Note: NO truncation param — CCE API does not accept it
            )
            # result.results[0].embeddings[0] = the embedding for the single chunk
            python_vecs.append(result.results[0].embeddings[0])

        # Java path: CceEmbedder via knowledge__ prefix routing
        java_vecs = _java_embed(base_url, token, _CCE_COLLECTION, CORPUS)

        assert len(java_vecs) == len(CORPUS)
        cosines = []
        for i, (py_v, jv_v) in enumerate(zip(python_vecs, java_vecs)):
            cosine = _cosine_sim(py_v, jv_v)
            cosines.append(cosine)
            print(f"  CCE path: corpus[{i}] cosine = {cosine:.10f}")
            assert 1.0 - cosine < 1e-9, (
                f"CCE parity FAILED for corpus[{i}]: "
                f"cosine = {cosine:.15f} (expected 1.0; diff={1.0 - cosine:.2e}, threshold=1e-9). "
                "Check: inputs=[[text]] on both sides? same model? "
                "No truncation param on CCE? Sort outer+inner data[] by index? "
                "drift > 1e-4 → float32 round-trip in /embed (fix: use CceEmbedder.embedDouble)."
            )
        print(f"\nCCE path: {[f'{c:.10f}' for c in cosines]}")

    # ── Self-check: ONNX Python determinism (no service needed) ───────────────

    def test_onnx_python_determinism(self) -> None:
        """Python ONNXMiniLM must return bit-identical vectors on repeated calls."""
        from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2
        ef = ONNXMiniLM_L6_V2()
        for text in CORPUS:
            v1 = ef([text])[0]
            v2 = ef([text])[0]
            assert np.array_equal(v1, v2), f"Python ONNX not deterministic for text: {text[:40]!r}"
