# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-144 P5a: nx doctor surfaces the two user-invisible local-embedder
states (the active embedder is resolved silently; the user never sees which
one ran).

State 1 — default 384: local mode, no ``nx init`` choice recorded, the
bundled 384-dim minilm is active. Nudge the user toward ``nx init`` for
bge-768.

State 2 — degraded bge: the user chose bge-768 via ``nx init`` but the
``[local]`` extra is missing, so ``_resolve_local_model`` silently falls
back to 384. This is a no-silent-fallback-for-correctness violation that
must surface as an actionable doctor finding, not a structlog line only.

Both are config-vs-availability divergences independent of stored-collection
dimensions, so they are tested against a pure helper rather than the daemon
probe.
"""
from __future__ import annotations

from nexus.db.local_ef import _TIER0_MODEL, _TIER1_MODEL
from nexus.health import local_embedder_advisory


class TestNoAdvisory:
    def test_explicit_bge_choice_available_is_silent(self) -> None:
        # chose bge AND bge is active — nothing to say.
        assert local_embedder_advisory(_TIER1_MODEL, _TIER1_MODEL) is None

    def test_explicit_minilm_choice_is_not_nagged(self) -> None:
        # user deliberately chose 384 — do not pester them to upgrade.
        assert local_embedder_advisory(_TIER0_MODEL, _TIER0_MODEL) is None

    def test_no_choice_but_bge_active_is_silent(self) -> None:
        # auto-selected bge (extra present, no explicit choice) — fine.
        assert local_embedder_advisory(None, _TIER1_MODEL) is None


class TestState1DefaultUpgradeNudge:
    def test_default_384_recommends_nx_init(self) -> None:
        r = local_embedder_advisory(None, _TIER0_MODEL)
        assert r is not None
        # soft warning (advisory), never fatal
        assert r.ok is False and r.warn is True and r.fatal is False
        assert "nx init" in " ".join(r.fix_suggestions)
        assert "bge-768" in (r.detail + " ".join(r.fix_suggestions))

    def test_detail_names_the_default_model(self) -> None:
        r = local_embedder_advisory(None, _TIER0_MODEL)
        assert r is not None
        assert "384" in r.detail


class TestState2DegradedBge:
    def test_bge_chosen_but_extra_missing_is_flagged(self) -> None:
        # choice = bge, but active resolved to 384 => silent degrade.
        r = local_embedder_advisory(_TIER1_MODEL, _TIER0_MODEL)
        assert r is not None
        # advisory, never fatal — a degraded embedder must not fail doctor.
        assert r.ok is False and r.warn is True and r.fatal is False
        joined = r.detail + " " + " ".join(r.fix_suggestions)
        # must name the degradation AND the actionable fix
        assert "384" in joined
        assert "nx init" in joined or "conexus[local]" in joined

    def test_state2_distinct_from_state1(self) -> None:
        s1 = local_embedder_advisory(None, _TIER0_MODEL)
        s2 = local_embedder_advisory(_TIER1_MODEL, _TIER0_MODEL)
        assert s1 is not None and s2 is not None
        # the degraded-bge message must call out the chosen-but-missing
        # state, not merely repeat the generic upgrade nudge.
        assert s1.detail != s2.detail
