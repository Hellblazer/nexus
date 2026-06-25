# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-168 P1.1 (bead nexus-zzact): service-mode catalog interface conformance.

The service-mode catalog client (`HttpCatalogClient`) must be signature-compatible
with the local canonical `Catalog` for every caller-facing method. When it is not,
service-mode `nx index repo` / catalog CLI calls either raise `TypeError` (a *breaking*
divergence) or — worse — are silently swallowed by a `**kwargs` on the client (a
*silent* divergence, e.g. `link_if_absent`), losing data with no error.

This is the recurrence guard for that entire class. It is introspection-based (no
hand-maintained per-method argument list): for every caller-facing `Catalog` method it
compares `inspect.signature` against `HttpCatalogClient`.

THE LOAD-BEARING PREDICATE (RDR-168 gate finding)
-------------------------------------------------
The check must NOT be "is the client call-compatible with the local arguments?", because
a `**kwargs` on the client satisfies that for *any* keyword argument — which is exactly
how the `link_if_absent` silent-data-loss class stays invisible. Instead, for every
EXPLICIT named parameter (positional-or-keyword / keyword-only, excluding `*args` /
`**kwargs`) on the local method, the client must expose a matching EXPLICIT named
parameter BY NAME. A `VAR_KEYWORD` (`**kwargs`) on the client does NOT satisfy it.

The contract is a one-directional MINIMUM: the client MAY carry extra service-only
params (the 6 BENIGN methods — e.g. `cross_model`, `legacy_grandfathered`,
`new_collection`); those are deliberate service capabilities and are NOT flagged.

TDD status
----------
RED by construction: the 19 currently-divergent methods (18 breaking + the
`link_if_absent` silent case) are marked `xfail(strict=True)`. As RDR-168 Phase 3
reconciles each client signature, its conformance assertion starts passing — `strict`
turns the resulting XPASS into a failure, forcing removal of the method from
`EXPECTED_NONCONFORMING` (the single source of truth) in lockstep. When the set is
empty the suite is fully GREEN with the permanent guard live.

Phase 2 (bead nexus-ja47l) refactors the enumerated surface from "all public `Catalog`
methods" to the explicit `CatalogReader` / `CatalogWriter` Protocol subset; the
predicate itself does not change.

SCOPE BOUNDARY (necessary, not sufficient)
------------------------------------------
This is a Python-only introspection test: it proves signature PARITY, not wire
correctness. Signature conformance is necessary but NOT sufficient for the silent class
specifically — a Phase 3 reconciliation that accepts `created_by` in the
`link_if_absent` signature but fails to SERIALIZE it into the HTTP request body produces
a GREEN conformance test and the same silent data loss at a lower layer. Phase 3 must
therefore add a live round-trip integration test asserting `created_by` / `from_span` /
`to_span` / `allow_dangling` reach the service (pre-filed follow-on; see RDR-168
Phase 3 / the P4 live MVV bead nexus-pwclh).
"""
from __future__ import annotations

import inspect
from collections.abc import Callable

import pytest

from nexus.catalog.catalog import Catalog
from nexus.catalog.http_catalog_client import HttpCatalogClient

# ── the gate-locked RED baseline ────────────────────────────────────────────────
#
# The 19 caller-facing methods whose `HttpCatalogClient` signature does not yet satisfy
# the canonical local `Catalog` signature, as audited on develop 2026-06-25
# (T2 nexus_rdr/168-research-1). 18 BREAKING (client missing/renamed a param the caller
# passes -> TypeError) + 1 SILENT (`link_if_absent`: client `**kwargs` swallows
# created_by/from_span/to_span/allow_dangling/meta with no error -> data loss).
#
# RDR-168 Phase 3 reconciliation removes each name here as the client method is brought
# to the canonical signature. The empty set is the GREEN end state.
EXPECTED_NONCONFORMING: frozenset[str] = frozenset(
    {
        # group 3A — collection / owner (indexing-path criticals, CA-4 core)
        "collection_for",
        "collection_for_repo",
        "ensure_owner_for_repo",
        "lookup_doc_id_by_collection_and_path",
        "list_by_collection",
        # group 3B — mutation / lifecycle
        "update_document_collection",
        "update_documents_collection_batch",
        "supersede_collection",
        # group 3C — links (incl. the SILENT case)
        "link",
        "link_if_absent",  # SILENT: **kwargs swallow — the load-bearing case
        "links_from",
        "links_to",
        "bulk_unlink",
        # group 3D — graph / resolve / reads
        "all_documents",
        "graph",
        "graph_many",
        "resolve_chash",
        "resolve_span",
        "is_initialized",
    }
)


def _public_methods(cls: type) -> dict[str, Callable]:
    """Public (non-underscore) instance/static methods of `cls`.

    NOTE: `inspect.isfunction` is False for `classmethod` descriptors (class access
    yields a bound method), so classmethods are deliberately excluded from the compared
    surface — they are a distinct factory call pattern. `test_no_unguarded_public_classmethods`
    guards that exclusion so a NEW shared classmethod forces a conscious decision rather
    than silently dropping out of conformance coverage.
    """
    return {
        name: member
        for name, member in inspect.getmembers(cls, predicate=inspect.isfunction)
        if not name.startswith("_")
    }


def _public_classmethods(cls: type) -> set[str]:
    return {
        name
        for name in dir(cls)
        if not name.startswith("_")
        and isinstance(inspect.getattr_static(cls, name), classmethod)
    }


def _explicit_named_params(method: Callable) -> list[str]:
    """Explicit named params of `method`, excluding `self`, `*args` and `**kwargs`.

    `VAR_KEYWORD` / `VAR_POSITIONAL` are deliberately excluded so that a `**kwargs` on
    the client does NOT count as satisfying a caller's named argument — the property the
    RDR-168 gate identified as load-bearing.
    """
    out: list[str] = []
    for param in inspect.signature(method).parameters.values():
        if param.name == "self":
            continue
        if param.kind in (
            inspect.Parameter.VAR_KEYWORD,
            inspect.Parameter.VAR_POSITIONAL,
        ):
            continue
        out.append(param.name)
    return out


def _missing_client_params(method_name: str) -> list[str]:
    """Local explicit params of `method_name` that the client does NOT expose by name.

    A non-empty result means the method is NON-CONFORMING under the predicate. The
    client's `**kwargs`, if any, is excluded from the satisfying set by
    `_explicit_named_params`, so the silent class is surfaced rather than hidden.
    """
    local = _LOCAL_METHODS[method_name]
    client = _CLIENT_METHODS[method_name]
    local_params = _explicit_named_params(local)
    client_params = set(_explicit_named_params(client))
    return [p for p in local_params if p not in client_params]


_LOCAL_METHODS = _public_methods(Catalog)
_CLIENT_METHODS = _public_methods(HttpCatalogClient)

# Caller-facing surface for Phase 1: every public `Catalog` method also present on the
# client. (The audit found 0 methods missing on the client.) Phase 2 (nexus-ja47l)
# narrows this to the explicit Protocol subset.
_CALLER_FACING: list[str] = sorted(
    name for name in _LOCAL_METHODS if name in _CLIENT_METHODS
)


def _compute_nonconforming() -> set[str]:
    return {name for name in _CALLER_FACING if _missing_client_params(name)}


# ── structural sanity ───────────────────────────────────────────────────────────


def test_client_is_not_missing_any_caller_facing_method() -> None:
    """Every public `Catalog` method exists on `HttpCatalogClient` (audit: 0 missing).

    A method missing entirely is a distinct, harder failure than a signature divergence;
    pin it so a future deletion is caught here rather than at runtime.
    """
    missing = sorted(name for name in _LOCAL_METHODS if name not in _CLIENT_METHODS)
    assert missing == [], (
        f"HttpCatalogClient is missing caller-facing Catalog methods: {missing}"
    )


def test_no_unguarded_public_classmethods() -> None:
    """Classmethods are excluded from the compared surface (`isfunction` is False).

    Pin the known set so a NEW public classmethod on either class trips here and forces
    a conscious decision about whether it belongs in the conformance surface, rather than
    silently dropping out of coverage. `Catalog.init` is the factory; the client has none.
    """
    assert _public_classmethods(Catalog) == {"init"}
    assert _public_classmethods(HttpCatalogClient) == set()


# ── exact-count RED baseline lock (==N, never >=) ────────────────────────────────


def test_nonconforming_set_matches_locked_baseline() -> None:
    """Lock the divergence set EXACTLY to the RDR-168 audit baseline.

    Exact equality (not `>=`) per feedback_exact_assertions_for_fixture_regression:
    - a NEW divergence (a 20th method, or a regression on a currently-conforming one)
      makes `actual` a superset -> FAIL (recurrence guard);
    - a Phase 3 fix that is NOT reflected in `EXPECTED_NONCONFORMING` makes `actual` a
      subset -> FAIL, forcing the frozenset to shrink in lockstep with the fix.
    """
    actual = _compute_nonconforming()
    assert actual == set(EXPECTED_NONCONFORMING), (
        "Catalog/HttpCatalogClient signature divergence drifted from the RDR-168 "
        f"baseline.\n  unexpected (new divergence): {sorted(actual - EXPECTED_NONCONFORMING)}"
        f"\n  reconciled (remove from EXPECTED_NONCONFORMING): "
        f"{sorted(EXPECTED_NONCONFORMING - actual)}"
    )


def test_nonconforming_count_is_locked() -> None:
    """The audited divergence count is exactly 19 (18 breaking + 1 silent)."""
    assert len(_compute_nonconforming()) == 19


# ── the load-bearing predicate, proven directly ──────────────────────────────────


def test_predicate_rejects_kwargs_swallow_for_link_if_absent() -> None:
    """`link_if_absent` is non-conforming BECAUSE the client `**kwargs` is excluded.

    This is the silent-data-loss case the RDR-168 gate flagged: a naive
    call-compatibility check would PASS here (the client's `**kwargs` absorbs any keyword
    argument), hiding the divergence. The predicate must instead report the local named
    params the client does not expose explicitly.
    """
    assert "link_if_absent" in _CALLER_FACING
    missing = _missing_client_params("link_if_absent")
    assert missing, (
        "predicate failed to surface the link_if_absent silent **kwargs swallow — a "
        "VAR_KEYWORD on the client must NOT satisfy a caller's named parameter"
    )
    # The client genuinely has a **kwargs (that is what makes it silent, not breaking)…
    client_kinds = {
        p.kind
        for p in inspect.signature(_CLIENT_METHODS["link_if_absent"]).parameters.values()
    }
    assert inspect.Parameter.VAR_KEYWORD in client_kinds
    # …yet the named caller params (e.g. created_by) are still reported as unserved.
    assert "created_by" in missing


def test_kwargs_does_not_satisfy_a_named_param_unit() -> None:
    """Isolated proof of the predicate property, independent of the live signatures."""

    def local(self, owner, created_by=None) -> None:  # noqa: ANN001
        ...

    def client_kwargs(self, **kwargs) -> None:  # noqa: ANN001, ANN003
        ...

    def client_explicit(self, owner, created_by=None) -> None:  # noqa: ANN001
        ...

    assert _explicit_named_params(local) == ["owner", "created_by"]
    # **kwargs contributes nothing to the satisfying set.
    assert _explicit_named_params(client_kwargs) == []
    assert _explicit_named_params(client_explicit) == ["owner", "created_by"]


# ── the permanent recurrence guard (per-method conformance) ──────────────────────


def _conformance_param(method_name: str):
    """One parametrize case; divergent methods carry a strict-xfail mark.

    `xfail(strict=True)` (declarative, so the assertion still runs): a divergent method
    XFAILs now, and the moment Phase 3 reconciles its signature the assertion passes ->
    XPASS -> strict turns that into a failure, forcing the name out of
    `EXPECTED_NONCONFORMING`. That is the RED->GREEN forcing function.
    """
    marks = (
        pytest.mark.xfail(
            strict=True,
            reason=(
                f"RDR-168 Phase 3 reconciliation pending for {method_name}() "
                "(remove from EXPECTED_NONCONFORMING when fixed)"
            ),
        )
        if method_name in EXPECTED_NONCONFORMING
        else ()
    )
    return pytest.param(method_name, marks=marks)


@pytest.mark.parametrize(
    "method_name", [_conformance_param(name) for name in _CALLER_FACING]
)
def test_method_signature_conforms(method_name: str) -> None:
    """`HttpCatalogClient.<m>` exposes every explicit named param of `Catalog.<m>`.

    RED-by-construction: the 19 audited divergences are `xfail(strict=True)`; each goes
    XPASS (→ failure → remove the xfail) when RDR-168 Phase 3 reconciles its signature.
    """
    missing = _missing_client_params(method_name)
    assert not missing, (
        f"HttpCatalogClient.{method_name}() is missing caller-facing params {missing} "
        f"that callers pass to Catalog.{method_name}() (a **kwargs does not satisfy them)"
    )
