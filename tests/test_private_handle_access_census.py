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
    # 2 -> 1: the `db.taxonomy._lock` / `.conn` reach-ins died with the raw
    # CatalogTaxonomy branches they guarded (nexus-i711w Stage 2 sub-stage C).
    # The survivor is `t3._client`, a T3 handle — unrelated to the T2 retirement.
    "src/nexus/commands/taxonomy_cmd.py": 1,
    # T3Database's own module: db._client_for / db._client on instances of the
    # class defined in this file. Self-access in substance, not syntax.
    "src/nexus/db/t3.py": 2,
    # 2 -> 0 (nexus-i711w Stage 2 sub-stage C): both `get_t3()._client` sites
    # lived in taxonomy_assign_batch_hook's raw arm, which the instance-based
    # is_service_backed check already returned before on every shipping
    # install. The arm is deleted, so the reach-ins are gone rather than
    # merely guarded. Entry kept at 0 rather than dropped: a named zero says
    # "this file was audited and is clean", which a missing key does not.
    "src/nexus/mcp_infra.py": 0,
}


def _scan_tree(path: Path) -> list[tuple[int, str, str]]:
    """(lineno, attr, receiver) for one file, NON-self receivers only.

    Split out of ``_non_self_private_accesses`` so the non-vacuity test can
    drive it with a known sample instead of asserting a floor on live debt.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, OSError, UnicodeDecodeError):
        return []
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
    return hits


def _non_self_private_accesses() -> dict[str, list[tuple[int, str, str]]]:
    """(lineno, attr, receiver) per file across SRC, for NON-self receivers."""
    found: dict[str, list[tuple[int, str, str]]] = {}
    for path in SRC.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        hits = _scan_tree(path)
        if hits:
            found[path.relative_to(REPO_ROOT).as_posix()] = hits
    return found


def test_scanner_is_not_vacuous(tmp_path: Path) -> None:
    """Prove the AST walk still matches, WITHOUT asserting a floor on live debt.

    This used to read ``total >= 5`` over the real tree. That conflated two
    different things — "the scanner works" and "the debt is large" — and the
    second is what the SQLite retirement exists to drive to zero. Each
    deletion pass pushed the count toward the floor, so the check's own
    failure mode became "the arc succeeded", and the only way to keep the
    suite green was to ratchet a non-vacuity assert downward: precisely the
    move the project bans (nexus-i711w Stage 2 sub-stage C).

    Re-grounded on a synthetic sample, it stays meaningful at a census of
    zero — which is where this ledger is headed.
    """
    sample = tmp_path / "sample.py"
    sample.write_text(
        "class Owner:\n"
        "    def use(self):\n"
        "        return self._client\n"        # self-access: must NOT match
        "\n"
        "def reach(other):\n"
        "    return other._client\n",           # non-self: MUST match
        encoding="utf-8",
    )
    hits = _scan_tree(sample)
    assert hits == [(6, "_client", "other")], (
        f"scanner no longer matches a known non-self private-handle access: {hits}. "
        "The attribute set or the AST walk has drifted, and every guard below "
        "would pass by doing nothing."
    )


def test_scanner_still_sees_the_live_tree() -> None:
    """Companion to the synthetic check: the walk is actually pointed at src/.

    A scanner that works on a tmp file but resolves SRC to nothing would still
    make the guards vacuous. This asserts reachability, not a debt floor, so
    it survives the census reaching zero.
    """
    assert any(SRC.rglob("*.py")), f"SRC does not resolve to python sources: {SRC}"


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
        # db/migrations.py entry removed — RDR-158 P4 Stage 4 (nexus-i711w):
        # the file is DELETED (a deleted file trivially holds zero).
        "src/nexus/db/t3_reidentify.py",
        "src/nexus/db/embed_migrate.py",
    ):
        assert rel not in live, (
            f"{rel} reacquired a private-handle reach at "
            f"{live.get(rel)} — this file was fixed in the nexus-at2ff sweep."
        )
