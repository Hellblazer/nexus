# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-223 P3.1 (nexus-z0o2p.23): no test reaches the three ownerless routes unless it says why.

The engine refuses a write to ``/v1/vectors/upsert-chunks``, ``/store-put`` and
``/upsert-reference-only`` whose chashes have no live manifest row (P3.2). A test that builds a chunk
ahead of its owner, or an orphan on purpose, uses ``tests/_chunk_seed.py`` instead. A dynamic probe
over the suite cannot prove that: it only sees the client's own request layer, in one process, so
raw ``httpx``/``urllib`` posts (two files, ``test_http_chash_integration`` and
``test_frecency_enehl_integration``, got past it) and subprocess or shell legs are invisible to it.
This is the static twin: it finds every CODE reference (string constants outside docstrings, calls of
the client methods) to those routes under ``tests/`` and ``service/native-smoke.sh``, and requires
each file to be in ``_ALLOWED`` with the reason that the reference cannot write an ownerless chunk
into a real engine.

A new test that needs an orphan does not add an entry; it calls ``seed_chunks_direct``. An entry is
for a file that stubs the transport, writes only chashes it owned first, or is itself about the route.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO = Path(__file__).resolve().parent.parent

_ROUTE = re.compile(
    r"/v1/vectors/(?:upsert-chunks|store-put|upsert-reference-only)"
    r"|^/(?:upsert-chunks|store-put|upsert-reference-only)$"
)
_METHODS = frozenset({
    "upsert_chunks", "upsert_chunks_with_embeddings",
    "upsert_reference_only", "upsert_reference_only_chunk",
})

_STUBBED = "transport stubbed (patched _post, loopback fake server, fake or in-memory client): never reaches a real engine"
_SPY = "names the route only to spy on or assert about traffic; writes nothing itself"
_OWNED = "writes only chashes it seeded and owned first, so the engine accepts it before and after P3.2"
_ROUTE_SUBJECT = "its subject is the route itself (nexus-z0o2p.24: pins the route's conflict write and its refusal of an ownerless first write)"

_ALLOWED: dict[str, str] = {
    "tests/_owner_write_double.py": "fake owner-write double over a fake T3; no engine",
    "tests/db/test_data_token_manager_e2e.py": _OWNED + " (seed_chunks_direct + give_chunks_a_live_owner)",
    "tests/db/test_xzeml_mint_armed_no_static_token.py": _OWNED + " (seed_chunks_direct + give_chunks_a_live_owner)",
    "tests/db/test_write_seam_gate_integration.py": _OWNED + " (_seed_owned_chunks into the hermetic container)",
    "tests/db/test_http_vector_client.py": _STUBBED,
    "tests/db/test_production_write_guard.py": "asserts the production-write guard fires on a write-shaped path; the guard is patched to raise before any request",
    "tests/e2e/lib/admission_load.py": _OWNED + " (re-posts chashes its own write_many owned)",
    "service/native-smoke.sh": _OWNED + " (re-posts chashes the same script owned through write_many)",
    "tests/e2e/local-service-gate.sh": _OWNED + " (re-posts a chash its own write_many owned) and the fixture text that tests the gate's own log parsing",
    "tests/e2e/local-index-memory-gate.sh": "greps the http_vector_upsert_chunks_request log event and carries it as fixture text; posts nothing",
    "tests/integration/test_rdr223_index_document_journey.py": _SPY,
    "tests/integration/test_rdr223_pdf_journey.py": _SPY,
    "tests/test_1jtob_edge_403_diagnostics.py": _STUBBED,
    "tests/test_35ok4_local_voyage_restart_remedy.py": _STUBBED,
    "tests/test_admission_load_gate.py": _STUBBED + " (a fake engine that refuses non-owned chashes)",
    "tests/test_chunk_seed.py": "parity oracle: drives the same input through the route and through seed_chunks_direct; " + _ROUTE_SUBJECT,
    "tests/test_collection_registration.py": _STUBBED + " (a fake VectorServiceError message)",
    "tests/test_etl_retry.py": _STUBBED,
    "tests/test_gtl01_upsert_ack_coverage.py": _STUBBED,
    "tests/test_http_vector_client_deadline_header.py": _STUBBED,
    "tests/test_http_vector_client_parity.py": _STUBBED,
    "tests/test_local_daemon_client_embed.py": _STUBBED + " (T3Database over a MagicMock client)",
    "tests/test_local_mode.py": _STUBBED + " (in-memory T3Database)",
    "tests/test_service_mode_cli_real_client.py": _STUBBED,
    "tests/test_shakeout_store_put_census.py": "route name inside a stub shell script's log line; posts nothing",
    "tests/test_t3.py": _STUBBED + " (in-memory T3Database)",
    "tests/test_vector_retry.py": _STUBBED,
    "tests/test_z0o2p16_store_put_note_writer.py": _SPY,
    "tests/test_z0o2p19_nxexp_import_combined_write.py": _SPY,
    "tests/test_z0o2p24_reembed_concurrent_supersede.py": _OWNED + "; the one chash that loses its owner mid-run is refused by the engine by design and the client resends the rest (a fake db covers the other branches)",
    "tests/test_znwc2_response_shape_trust.py": _STUBBED,
}


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for n in ast.walk(tree):
        if (
            isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and n.body
            and isinstance(n.body[0], ast.Expr)
            and isinstance(getattr(n.body[0], "value", None), ast.Constant)
        ):
            ids.add(id(n.body[0].value))
    return ids


def _python_hits(path: Path) -> int:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return 0
    doc = _docstring_ids(tree)
    hits = 0
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in doc and _ROUTE.search(n.value):
            hits += 1
        elif isinstance(n, ast.Call):
            f = n.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if name in _METHODS:
                hits += 1
    return hits


def _shell_hits(path: Path) -> int:
    return sum(
        1
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
        if not line.lstrip().startswith("#") and _ROUTE.search(line)
    )


def _scan() -> dict[str, int]:
    found: dict[str, int] = {}
    files = [p for p in (REPO / "tests").rglob("*") if p.suffix in (".py", ".sh") and p.is_file()]
    files.append(REPO / "service" / "native-smoke.sh")
    for p in files:
        if p == Path(__file__):
            continue
        n = _python_hits(p) if p.suffix == ".py" else _shell_hits(p)
        if n:
            found[str(p.relative_to(REPO))] = n
    return found


def test_every_reference_to_an_ownerless_route_is_allowlisted_with_a_reason() -> None:
    found = _scan()
    offenders = sorted(set(found) - set(_ALLOWED))
    assert not offenders, (
        "these files reference /v1/vectors/upsert-chunks, /store-put, /upsert-reference-only or an "
        "upsert_chunks* client method in code. A test that needs an orphan or a chunk ahead of its "
        "owner calls tests/_chunk_seed.py::seed_chunks_direct (tests/AGENTS.md). Only a file that "
        f"stubs the transport, writes owned chashes, or is about the route belongs in _ALLOWED: {offenders}"
    )


def test_the_allowlist_has_no_stale_entries_and_every_reason_is_real() -> None:
    found = _scan()
    stale = sorted(set(_ALLOWED) - set(found))
    assert not stale, f"allowlisted files with no code reference left (remove the entry): {stale}"
    for path, reason in _ALLOWED.items():
        assert len(reason.strip()) > 20, f"{path}: reason too short to be a justification"


def test_the_scan_is_not_vacuous() -> None:
    found = _scan()
    assert len(found) >= 20, f"the scan found only {len(found)} files; the walk is broken"
    assert "tests/db/test_http_vector_client.py" in found
    assert "tests/_chunk_seed.py" not in found, "the seeder must not reference the routes in code"
    assert "service/native-smoke.sh" in found, "native-smoke re-posts owned chashes; the scan must see it"
