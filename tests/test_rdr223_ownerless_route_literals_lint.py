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

#: A part the fold cannot resolve (a variable, a call, a formatted value) becomes this character, so a
#: route assembled as ``base + "/v1/vectors/upsert-" + "chunks"`` still reads as the route.
_UNKNOWN = "\x00"

_ROUTE = re.compile(
    r"/v1/vectors/(?:upsert-chunks|store-put|upsert-reference-only)"
    r"|(?:^|\x00)/(?:upsert-chunks|store-put|upsert-reference-only)$"
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
    "service/native-smoke.sh": _OWNED + " (re-posts chashes the same script owned through write_many)",
    "tests/e2e/local-service-gate.sh": _OWNED + " (re-posts a chash its own write_many owned), sends ONE deliberate ownerless upsert-chunks as the nexus-z0o2p.24 negative control, and carries fixture text that tests the gate's own log parsing",
    "tests/integration/test_rdr223_index_document_journey.py": _SPY,
    "tests/integration/test_rdr223_pdf_journey.py": _SPY,
    "tests/test_1jtob_edge_403_diagnostics.py": _STUBBED,
    "tests/test_35ok4_local_voyage_restart_remedy.py": _STUBBED,
    "tests/test_chunk_seed.py": "parity oracle: drives the same input through the route and through seed_chunks_direct; " + _ROUTE_SUBJECT,
    "tests/test_collection_registration.py": _STUBBED + " (a fake VectorServiceError message)",
    "tests/test_etl_retry.py": _STUBBED,
    "tests/test_gtl01_upsert_ack_coverage.py": _STUBBED,
    "tests/test_http_vector_client_deadline_header.py": _STUBBED,
    "tests/test_http_vector_client_parity.py": _STUBBED,
    "tests/test_local_daemon_client_embed.py": _STUBBED + " (T3Database over a MagicMock client)",
    "tests/test_local_mode.py": _STUBBED + " (in-memory T3Database)",
    "tests/test_service_mode_cli_real_client.py": _STUBBED,
    "tests/test_t3.py": _STUBBED + " (in-memory T3Database)",
    "tests/test_vector_retry.py": _STUBBED,
    "tests/test_z0o2p16_store_put_note_writer.py": _SPY,
    "tests/test_z0o2p19_nxexp_import_combined_write.py": _SPY,
    "tests/test_z0o2p24_client_version_header.py": _STUBBED + " (the opener is replaced; the request is captured, never sent)",
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


def _fold(node: ast.AST) -> str | None:
    """The string an expression builds, with ``_UNKNOWN`` for parts it cannot resolve, or None when
    the expression is not string-shaped. Folds ``"a" + "b"``, which a lint that looks only at
    ``ast.Constant`` nodes misses (a split literal reaches the route unseen).

    An f-string is folded too (its literal parts joined, each ``{...}`` hole an ``_UNKNOWN``). A route
    inside a bare f-string is also visible through the f-string's own ``Constant`` parts, so that case
    does not need the fold; an f-string that is an OPERAND of ``+`` does: ``"/v1/vectors/" +
    f"upsert-chunks"`` has no Constant that is the route, and only the ``JoinedStr`` branch below
    assembles it (round 3 mutation check: with the branch removed that file and the ``h + ... +
    f"..."`` form go unflagged). Round 2 deleted this branch as dead and its commit message said every
    fixture still flagged the same files; that was wrong, because no fixture combined ``+`` with an
    f-string."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        return "".join(
            v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else _UNKNOWN
            for v in node.values
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _fold(node.left), _fold(node.right)
        if left is None and right is None:
            return None
        return (left if left is not None else _UNKNOWN) + (right if right is not None else _UNKNOWN)
    return None


def _python_hits(path: Path) -> int:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return 0
    doc = _docstring_ids(tree)
    hits = 0
    for n in ast.walk(tree):
        if id(n) in doc:
            continue
        folded = _fold(n) if isinstance(n, (ast.Constant, ast.BinOp)) else None
        if folded is not None and _ROUTE.search(folded):
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


def _scan(repo: Path = REPO, me: Path = Path(__file__)) -> dict[str, int]:
    """``repo`` is a RESOLVED path, so the walk yields resolved files; ``me`` is compared resolved too,
    because ``__file__`` keeps a symlinked checkout's spelling and an unresolved compare would count
    this file's own route literals as an offender."""
    found: dict[str, int] = {}
    files = [p for p in (repo / "tests").rglob("*") if p.suffix in (".py", ".sh") and p.is_file()]
    smoke = repo / "service" / "native-smoke.sh"
    if smoke.is_file():
        files.append(smoke)
    here = me.resolve()
    for p in files:
        if p.resolve() == here:
            continue
        n = _python_hits(p) if p.suffix == ".py" else _shell_hits(p)
        if n:
            found[str(p.relative_to(repo))] = n
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


def test_a_split_or_formatted_route_literal_is_seen(tmp_path: Path) -> None:
    """A route built by ``+`` (a Constant-only walk misses it), a route in an f-string (seen through its
    literal part), an f-string operand of ``+`` (the ``JoinedStr`` fold), and a relative route after a part the fold cannot resolve (the ``\\x00`` alternative)."""
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_split.py").write_text('URL = "/v1/vectors/" + "upsert-chunks"\n')
    (tests / "test_split3.py").write_text('def u(base):\n    return base + "/v1/vectors/upsert-" + "chunks"\n')
    (tests / "test_fstring.py").write_text('def u(h):\n    return f"{h}/v1/vectors/store-put"\n')
    (tests / "test_handler_relative.py").write_text('def u(p):\n    return p + "/upsert-chunks"\n')
    # Reachable ONLY through the ``\x00`` alternative of ``_ROUTE``: no Constant is the route, and the
    # fold is ``<unknown>/upsert-chunks``, a relative route after a part the fold could not resolve.
    (tests / "test_relative_split.py").write_text('def u(p):\n    return p + "/upsert-" + "chunks"\n')
    # An f-string that is an OPERAND of ``+`` is reachable only through the ``JoinedStr`` fold.
    (tests / "test_fstring_operand.py").write_text('URL = "/v1/vectors/" + f"upsert-chunks"\n')
    (tests / "test_fstring_operand3.py").write_text('def u(h):\n    return h + "/v1/vectors/up" + f"sert-chunks"\n')
    (tests / "test_clean.py").write_text('URL = "/v1/vectors/" + "search"\nDOC = f"{1}/v1/catalog/x"\n')
    found = _scan(repo=tmp_path.resolve(), me=Path("/nonexistent/me.py"))
    assert set(found) == {
        "tests/test_split.py", "tests/test_split3.py", "tests/test_fstring.py", "tests/test_handler_relative.py",
        "tests/test_relative_split.py", "tests/test_fstring_operand.py", "tests/test_fstring_operand3.py",
    }


def test_the_self_exclusion_holds_under_a_symlinked_checkout(tmp_path: Path) -> None:
    """``__file__`` under a symlinked checkout is not the resolved path the walk yields; the lint's own
    route literals must still not count against it."""
    real = tmp_path / "real"
    (real / "tests").mkdir(parents=True)
    me_real = real / "tests" / "test_lint_itself.py"
    me_real.write_text('ROUTE = "/v1/vectors/upsert-chunks"\n')
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    me_via_link = link / "tests" / "test_lint_itself.py"
    assert me_via_link != me_real.resolve()  # the premise: the two spellings differ
    assert _scan(repo=real.resolve(), me=me_via_link) == {}
    assert _scan(repo=real.resolve(), me=Path("/nonexistent/me.py")) == {"tests/test_lint_itself.py": 1}
