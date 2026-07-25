# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""No reaching into ANOTHER object's private client handle (nexus-cwtwx).

THE BUG CLASS THIS FREEZES. Production code accesses ``._client`` /
``._client_for`` on a handle that only has it in TESTS::

    result = cat.resolve_span(span, collection, t3._client)

``make_t3()`` returns ``HttpVectorClient`` in production — no ``_client``.
``T3Database``, the test facade built with an injected client, HAS one. So the
call raised ``AttributeError`` in production only, and a nearby broad ``except``
read that as an environment condition ("T3 unavailable — skip validation"). The
code silently did nothing while 13,000 tests proved it worked, because the tests
ran the one handle shape that has the attribute.

On 2026-07-25 that produced NINE sites across two independent sweeps, including
an upgrade blocker (``migrations.py`` — every collection SKIPPED, then a hard
fail, so ``nx upgrade`` promised a retry that could never succeed), two CLI
verbs that errored on every collection, and chash-span validation that had been
dead in production for an unknown period. It also produced THREE test fixtures
that encoded a handle shape which has never existed (``tests/test_catalog.py``,
``tests/test_projection_quality.py``, ``tests/mcp/test_remediate_tool.py``).

WHY THE EXISTING LINT COULD NOT SEE ANY OF IT. ``storage_boundary_lint``'s
``CLIENT_FOR_ALLOWLIST_PREFIXES`` (GH #1373, hard-enforced at baseline 0)
allowlists by the FILE the access appears in — ``src/nexus/db/``. It says
nothing about WHICH HANDLE arrives. A ``db/``-internal helper called from
``commands/`` with a ``make_t3()`` handle is invisible to it by construction,
which is exactly ``db/t3_reidentify.py``.

WHY A CENSUS RATHER THAN DATAFLOW. Of the nine sites, FIVE arrived as function
PARAMETERS (``t3_db._client``, ``db._client_for``) and one receiver was named
just ``db``. Local dataflow analysis would have missed most of them, and a
name-based heuristic would be guesswork. Receiver identity is the wrong thing to
chase; the ACT — one object reaching into another's private client handle — is
the thing to freeze.

SELF-ACCESS IS NOT THE BUG AND IS NOT COUNTED. ``self._client`` inside the class
that owns the attribute is ordinary encapsulation; 78 of the 85 raw matches in
``src/`` are exactly that. Counting them would bury seven real sites in noise
and the census would be abandoned as unusable. This scanner counts only
NON-``self`` receivers.
"""
from __future__ import annotations

import ast
import pathlib

REPO_ROOT = pathlib.Path(__file__).parent.parent
SRC = REPO_ROOT / "src" / "nexus"

#: Private handle attributes that belong to a store/client implementation and
#: must not be reached for from outside it.
_PRIVATE_HANDLE_ATTRS = frozenset({"_client", "_client_for"})

#: 2026-07-25 census of NON-self private-handle accesses, per file.
#: Every entry is a site that was VERIFIED legitimate at the time of freezing.
#: This may only shrink. A new entry means someone reached into another
#: object's private handle, which is the nexus-at2ff class.
PRIVATE_HANDLE_CENSUS: dict[str, int] = {
    # Guarded: is_service_backed(t3) returns before this line, so it only ever
    # sees the legacy chroma-backed T3Database where ._client is correct. This
    # exact site was changed by pattern during the at2ff sweep and REVERTED —
    # it is the false positive that proves receiver-blind fixing is wrong too.
    "src/nexus/commands/catalog.py": 1,
    # Guarded: :86 by is_service_backed(t3); :1200 by
    # _require_supported_taxonomy_backend(t3, db.taxonomy) refusing the
    # split-backend config before the raw path.
    "src/nexus/commands/taxonomy_cmd.py": 2,
    # T3Database's own module: db._client_for / db._client on instances of the
    # class defined in this file. Self-access in substance, not syntax.
    "src/nexus/db/t3.py": 2,
    # Guarded: instance-based check (HttpVectorClient has no ._client) returns
    # cleanly before both sites; taxonomy-via-chroma is unsupported on service.
    "src/nexus/mcp_infra.py": 2,
}


def _non_self_private_accesses() -> dict[str, list[tuple[int, str, str]]]:
    """(lineno, attr, receiver) per file, for NON-self receivers only."""
    found: dict[str, list[tuple[int, str, str]]] = {}
    for path in SRC.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, OSError, UnicodeDecodeError):
            continue
        hits: list[tuple[int, str, str]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            if node.attr not in _PRIVATE_HANDLE_ATTRS:
                continue
            recv = node.value
            # self.<attr> — the owning class using its own handle.
            if isinstance(recv, ast.Name) and recv.id == "self":
                continue
            name = (
                recv.id if isinstance(recv, ast.Name)
                else getattr(getattr(recv, "func", None), "id", None) or "<expr>"
            )
            hits.append((node.lineno, node.attr, name))
        if hits:
            found[path.relative_to(REPO_ROOT).as_posix()] = hits
    return found


def test_scanner_is_not_vacuous() -> None:
    """Every assertion below iterates the scan. If the AST walk silently
    stopped matching (attribute renamed, path drift), an empty result would
    make the real guards pass by doing nothing."""
    live = _non_self_private_accesses()
    assert live, "scanner found ZERO private-handle accesses — it is broken"
    total = sum(len(v) for v in live.values())
    assert total >= 5, f"implausibly few sites ({total}); scan likely broken"


def test_self_access_is_excluded() -> None:
    """The exclusion is load-bearing: without it the census is ~85 entries of
    ordinary encapsulation and the 7 real ones are invisible."""
    live = _non_self_private_accesses()
    for rel, hits in live.items():
        for lineno, attr, recv in hits:
            assert recv != "self", f"{rel}:{lineno} self.{attr} leaked into the scan"


def test_no_new_private_handle_reach() -> None:
    """The guard. A NEW non-self ``._client`` / ``._client_for`` access is the
    nexus-at2ff class and fails here rather than silently doing nothing in
    production for an unknown period."""
    live = {f: len(h) for f, h in _non_self_private_accesses().items()}
    grown = sorted(
        f"{f}: {live.get(f, 0)} > {PRIVATE_HANDLE_CENSUS.get(f, 0)}"
        for f in live.keys() | PRIVATE_HANDLE_CENSUS.keys()
        if live.get(f, 0) > PRIVATE_HANDLE_CENSUS.get(f, 0)
    )
    assert not grown, (
        f"private-handle reach GREW at {grown}.\n"
        "Production make_t3() returns HttpVectorClient, which has NO ._client "
        "or ._client_for — only the T3Database TEST facade does. Reaching for "
        "them raises AttributeError in production only, and any nearby broad "
        "except turns that into a silent no-op that tests cannot see "
        "(nexus-at2ff produced 9 such sites in one day, including an upgrade "
        "blocker).\n"
        "Pass the HANDLE itself: both HttpVectorClient and T3Database expose "
        "get_collection / get_or_create_collection directly. If the access is "
        "genuinely guarded (is_service_backed / an instance check that returns "
        "first), add it to PRIVATE_HANDLE_CENSUS with the guard named."
    )


def test_census_has_no_stale_entries() -> None:
    """Exact-census discipline, matching test_no_new_sqlite: a count that no
    longer matches reality is a lie about the debt, so shrinking must be
    recorded rather than left as slack that hides a later regrowth."""
    live = {f: len(h) for f, h in _non_self_private_accesses().items()}
    shrunk = sorted(
        f"{f}: {live.get(f, 0)} < {PRIVATE_HANDLE_CENSUS.get(f, 0)}"
        for f in live.keys() | PRIVATE_HANDLE_CENSUS.keys()
        if live.get(f, 0) < PRIVATE_HANDLE_CENSUS.get(f, 0)
    )
    assert not shrunk, (
        f"stale census entry {shrunk}: the access was removed (good) — lower "
        "the count so the frozen ledger stays exact."
    )


def test_the_fixed_sites_stay_fixed() -> None:
    """Named regression pins for the nine sites fixed on 2026-07-25.

    The census alone would let a file re-acquire a reach as long as another in
    the same file went away. These files must hold ZERO.
    """
    live = _non_self_private_accesses()
    for rel in (
        "src/nexus/catalog/catalog_links.py",
        "src/nexus/catalog/orphan_backfill.py",
        "src/nexus/mcp/catalog.py",
        "src/nexus/commands/catalog_cmds/orphan_backfill.py",
        "src/nexus/db/migrations.py",
        "src/nexus/db/t3_reidentify.py",
        "src/nexus/db/embed_migrate.py",
    ):
        assert rel not in live, (
            f"{rel} reacquired a private-handle reach at "
            f"{live.get(rel)} — this file was fixed in the nexus-at2ff sweep."
        )
