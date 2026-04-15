# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pin tests for ``docs/plan-authoring-guide.md`` (nexus-05i.8 / P4e).

Two contracts:

  * The guide exists at the documented path so Phase 5 skills and
    the ``verb:plan-author`` meta-seed can reference it by path.
  * The guide covers the vocabulary that SC-17's plan_match retrieval
    will semantically match against (``plan_match("how do I write a
    plan")`` → this file). Catalog indexing happens at ``nx catalog
    setup`` via the ``docs/`` sweep; this test verifies the *content*
    is dense enough to match those queries and mentions every
    downstream concept the RDR promises.

The ``plan_match`` retrieval assertion (≥ 0.80 confidence per SC-17)
is intentionally deferred to the end-to-end demo bead — unit tests
can't spin a representative ChromaDB session without heavyweight
fixtures. What we can pin here is that every semantic anchor the
end-to-end query would hit is in the file.
"""
from __future__ import annotations

from pathlib import Path


GUIDE_PATH = (
    Path(__file__).resolve().parents[1] / "docs" / "plan-authoring-guide.md"
)


def test_guide_exists() -> None:
    assert GUIDE_PATH.exists(), f"missing {GUIDE_PATH}"


def test_guide_is_non_trivial_length() -> None:
    """A guide under 1 KB almost certainly isn't dense enough to
    anchor a semantic match."""
    assert GUIDE_PATH.stat().st_size > 2_000


def test_guide_covers_four_axis_plan_match_contract() -> None:
    """Guide mentions intent, description, scope_preference, bindings —
    the four axes of the ``plan_match`` signature."""
    text = GUIDE_PATH.read_text()
    for anchor in ("intent", "description", "bindings", "scope"):
        assert anchor in text, f"plan_match anchor missing: {anchor}"


def test_guide_covers_phase2_scope_field() -> None:
    """Phase 2 scope routing (taxonomy_domain, topic, prose vs code)."""
    text = GUIDE_PATH.read_text()
    for anchor in ("taxonomy_domain", "prose", "code", "topic"):
        assert anchor in text, f"Phase 2 anchor missing: {anchor}"


def test_guide_covers_phase3_traverse_operator() -> None:
    """Phase 3 traverse operator: seeds, depth, direction, purpose /
    link_types, collections output, 500-node cap."""
    text = GUIDE_PATH.read_text()
    for anchor in (
        "traverse", "seeds", "depth", "direction",
        "purpose", "link_types", "collections",
        "_MAX_GRAPH_NODES",
    ):
        assert anchor in text, f"Phase 3 anchor missing: {anchor}"


def test_guide_covers_phase4a_schema_rules() -> None:
    """Phase 4a schema: canonical_dimensions_json dedup, SC-16
    mutual exclusion, required verb+scope pins."""
    text = GUIDE_PATH.read_text()
    for anchor in (
        "canonical_dimensions_json", "verb", "scope", "strategy",
        "dimensions", "required_bindings", "default_bindings",
    ):
        assert anchor in text, f"Phase 4a anchor missing: {anchor}"


def test_guide_covers_lifecycle_tiers() -> None:
    """Every lifecycle tier (personal → rdr → project → repo → global)
    named so agents can locate their target YAML path."""
    text = GUIDE_PATH.read_text()
    for tier in ("personal", "rdr-", "project", "repo", "global"):
        assert tier in text, f"lifecycle tier missing: {tier}"


def test_guide_forward_references_rdr_079() -> None:
    """Lifecycle ops (plan_promote CLI, audit CLI, RDR-close hooks)
    are deferred to RDR-079; the guide must say so explicitly so an
    agent reading it doesn't try to invoke them."""
    text = GUIDE_PATH.read_text()
    assert "RDR-079" in text


def test_guide_references_companion_docs() -> None:
    """Readers should be able to hop to the link-types + purposes
    references from this guide."""
    text = GUIDE_PATH.read_text()
    assert "catalog-link-types.md" in text
    assert "catalog-purposes.md" in text


def test_guide_covers_var_and_stepref_resolution() -> None:
    """``$var`` and ``$stepN.<field>`` substitution rules are a
    load-bearing runtime contract; authors need to know them."""
    text = GUIDE_PATH.read_text()
    assert "$var" in text
    assert "$stepN" in text or "$step1" in text
