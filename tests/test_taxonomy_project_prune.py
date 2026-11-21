"""GH #1528 (nexus-4tfxp): ``nx taxonomy project --prune-below-threshold`` is
the recovery path for a projection pass that admitted weak matches: one
engine-side delete per source collection, on the stored raw cosine, at that
collection's threshold (RDR-229 P2.4: its catalog row's content type, so the
walk is per full collection name and never per name prefix)."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from nexus.commands.taxonomy_cmd import taxonomy


def _wire(
    monkeypatch, pruned: list[tuple[str, float]], removed: int,
    *, rows: dict[str, str] | None = None, distinct: list[str] | None = None,
    unreadable: bool = False, exact: bool = False, exact_log: list[bool] | None = None,
    unreadable_names: tuple[str, ...] = (), projection_counts: dict[str, int] | None = None,
    probe: bool = True, echo: str | None = "auto",
    probe_log: list | None = None, probe_echo: str | None = "same", probe_raises: bool = False,
) -> None:
    """*rows* maps collection name -> catalog content_type (the live listing).
    *unreadable* makes the listing and every row read raise, as an outage does;
    *unreadable_names* makes the row read raise for those names only.
    *projection_counts* is what the store answers for the distinct ``source_collection``
    values of the projection rows. *echo* is the ``match`` the engine's reply carries:
    ``"auto"`` echoes the mode asked for (a current engine), ``None`` is a reply with no
    ``match`` key (an engine without the echo), any other string is sent as is.
    *exact* is what the engine's exact-name prune capability probe answers; *exact_log*
    collects the ``exact`` flag of each real prune call. The verification prune sent before the
    first exact prune (a ``nexus-exact-probe-<hex>`` name) is recorded in *probe_log* as
    ``(name, threshold, exact, real prunes already made)`` and answers *probe_echo* (``"same"``:
    as *echo* does), or raises when *probe_raises*."""
    rows = rows or {}
    if probe:  # False: the test installs its own catalog reader, so the real probe runs
        monkeypatch.setattr("nexus.commands.taxonomy_cmd.prune_supports_exact", lambda: exact, raising=False)

    class _Taxonomy:
        def get_distinct_collections(self) -> list[str]:
            return list(distinct or [])

        def get_projection_counts_by_collection(self) -> dict[str, int]:
            return dict(projection_counts or {})

        def prune_projection_below(self, prefix: str, thr: float, *, exact: bool = False) -> int:
            if prefix.startswith("nexus-exact-probe-"):  # the verification prune: matches no collection
                if probe_log is not None:
                    probe_log.append((prefix, thr, exact, [p[0] for p in pruned]))
                if probe_raises:
                    raise RuntimeError("engine unreachable")
                mode = ("exact" if exact else "prefix") if probe_echo == "same" and echo == "auto" else (
                    echo if probe_echo == "same" else probe_echo)
                return 0, mode
            pruned.append((prefix, round(thr, 2)))
            if exact_log is not None:
                exact_log.append(exact)
            mode = ("exact" if exact else "prefix") if echo == "auto" else echo
            return removed, mode

    class _Db:
        taxonomy = _Taxonomy()

        def close(self) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            self.close()

    monkeypatch.setattr("nexus.commands.taxonomy_cmd._T2Database", lambda *_a, **_k: _Db())
    monkeypatch.setattr("nexus.commands.taxonomy_cmd._command_shared_t2_client", lambda: None)
    monkeypatch.setattr("nexus.db.make_t3", lambda: object())
    def _listing():
        if unreadable:
            raise RuntimeError("listing unreadable")
        return sorted(rows)

    def _row(name, *, refresh=True, strict=False):
        if unreadable or name in unreadable_names:
            raise RuntimeError("listing unreadable")
        return {"content_type": rows[name]} if name in rows else None

    monkeypatch.setattr("nexus.mcp_infra.get_live_collection_names", _listing)
    monkeypatch.setattr("nexus.mcp_infra.get_collection_row", _row)


def test_prune_with_backfill_walks_every_collection_at_its_rows_threshold(monkeypatch) -> None:
    pruned: list[tuple[str, float]] = []
    _wire(
        monkeypatch, pruned, removed=5,
        rows={"code__a": "code", "knowledge__b": "knowledge", "docs__c": "docs", "rdr__d": "rdr"},
    )
    result = CliRunner().invoke(taxonomy, ["project", "--backfill", "--prune-below-threshold"])
    assert result.exit_code == 0, result.output
    assert pruned == [("code__a", 0.7), ("docs__c", 0.55), ("knowledge__b", 0.5), ("rdr__d", 0.55)]
    assert "Pruned 20 projection assignment(s)." in result.output


def test_prune_with_backfill_unions_collections_with_topics_and_no_row_uses_the_name(monkeypatch) -> None:
    pruned: list[tuple[str, float]] = []
    _wire(monkeypatch, pruned, removed=1, rows={"code__a": "code"}, distinct=["knowledge__gone", "code__a"])
    result = CliRunner().invoke(taxonomy, ["project", "--backfill", "--prune-below-threshold"])
    assert result.exit_code == 0, result.output
    assert pruned == [("code__a", 0.7), ("knowledge__gone", 0.5)]


def test_prune_with_backfill_survives_an_unreadable_listing(monkeypatch) -> None:
    pruned: list[tuple[str, float]] = []
    _wire(monkeypatch, pruned, removed=1, distinct=["docs__only-topics"])

    def _boom():
        raise RuntimeError("listing unreadable")

    monkeypatch.setattr("nexus.mcp_infra.get_live_collection_names", _boom)
    result = CliRunner().invoke(taxonomy, ["project", "--backfill", "--prune-below-threshold"])
    assert result.exit_code == 0, result.output
    assert pruned == [("docs__only-topics", 0.55)]


def test_prune_with_backfill_explicit_threshold_applies_to_every_collection(monkeypatch) -> None:
    pruned: list[tuple[str, float]] = []
    _wire(monkeypatch, pruned, removed=0, rows={"code__a": "code", "knowledge__b": "knowledge"})
    result = CliRunner().invoke(
        taxonomy, ["project", "--backfill", "--prune-below-threshold", "--threshold", "0.6"],
    )
    assert result.exit_code == 0, result.output
    assert pruned == [("code__a", 0.6), ("knowledge__b", 0.6)]


def test_prune_takes_a_prefix_name_down_to_the_lowest_threshold_of_its_extensions(monkeypatch) -> None:
    """The engine prunes by name prefix: ``code__1-1__m__v1`` at its own 0.70 would also delete
    the rows of ``code__1-1__m__v10`` (a knowledge row, 0.50) between 0.50 and 0.70. So ``v1`` is
    pruned at the lowest threshold among it and its longer-named siblings, 0.50: that deletes
    nothing ``v10``'s own pass would keep, and the summary says one collection was skipped."""
    pruned: list[tuple[str, float]] = []
    _wire(
        monkeypatch, pruned, removed=1,
        rows={"code__1-1__m__v1": "code", "code__1-1__m__v10": "knowledge"},
    )
    result = CliRunner().invoke(taxonomy, ["project", "--backfill", "--prune-below-threshold"])
    assert result.exit_code == 0, result.output
    assert pruned == [("code__1-1__m__v1", 0.5), ("code__1-1__m__v10", 0.5)]
    assert "code__1-1__m__v1: pruned only below 0.50" in result.output
    assert "1 skipped" in result.output


def test_prune_equal_thresholds_on_a_prefix_pair_prune_both_and_skip_none(monkeypatch) -> None:
    """The common prefix pair: legacy ``docs__art`` and ``docs__art-architecture`` share 0.55.
    Neither is skipped or lowered (nexus-p2m1x.9 review I1: ``<`` must not become ``<=``)."""
    pruned: list[tuple[str, float]] = []
    _wire(monkeypatch, pruned, removed=1, rows={"docs__art": "docs", "docs__art-architecture": "docs"})
    result = CliRunner().invoke(taxonomy, ["project", "--backfill", "--prune-below-threshold"])
    assert result.exit_code == 0, result.output
    assert pruned == [("docs__art", 0.55), ("docs__art-architecture", 0.55)]
    assert "skipped" not in result.output


def test_prune_equal_explicit_threshold_on_a_prefix_pair_prunes_both(monkeypatch) -> None:
    pruned: list[tuple[str, float]] = []
    _wire(monkeypatch, pruned, removed=1, rows={"docs__art": "docs", "docs__art-architecture": "knowledge"})
    result = CliRunner().invoke(
        taxonomy, ["project", "--backfill", "--prune-below-threshold", "--threshold", "0.6"],
    )
    assert result.exit_code == 0, result.output
    assert pruned == [("docs__art", 0.6), ("docs__art-architecture", 0.6)]
    assert "skipped" not in result.output


def test_prune_sweep_refuses_an_unreadable_row_read_and_deletes_nothing(monkeypatch) -> None:
    """Prune is the one consumer that deletes: on an unreadable listing it must not delete on the
    name guess (a code__-named collection with a knowledge row would go at 0.70, not 0.50)."""
    pruned: list[tuple[str, float]] = []
    _wire(monkeypatch, pruned, removed=1, distinct=["code__guess", "docs__guess"], unreadable=True)
    result = CliRunner().invoke(taxonomy, ["project", "--backfill", "--prune-below-threshold"])
    assert pruned == []
    assert "code__guess: refused" in result.output and "docs__guess: refused" in result.output
    assert "2 refused" in result.output
    assert "--threshold" in result.output
    assert result.exit_code != 0, "a sweep that pruned nothing and refused everything is not a success"


def test_prune_sweep_with_explicit_threshold_prunes_on_an_unreadable_listing(monkeypatch) -> None:
    pruned: list[tuple[str, float]] = []
    _wire(monkeypatch, pruned, removed=1, distinct=["code__guess"], unreadable=True)
    result = CliRunner().invoke(
        taxonomy, ["project", "--backfill", "--prune-below-threshold", "--threshold", "0.6"],
    )
    assert result.exit_code == 0, result.output
    assert pruned == [("code__guess", 0.6)]
    assert "refused" not in result.output


def test_prune_single_collection_refuses_an_unreadable_row_read(monkeypatch) -> None:
    pruned: list[tuple[str, float]] = []
    _wire(monkeypatch, pruned, removed=1, unreadable=True)
    result = CliRunner().invoke(taxonomy, ["project", "code__guess", "--prune-below-threshold"])
    assert pruned == []
    assert result.exit_code != 0
    assert "refused" in result.output and "--threshold" in result.output


def test_prune_single_collection_applies_the_prefix_collision_floor(monkeypatch) -> None:
    """Single-source prune is subject to the same engine prefix match (review S1, critic S3)."""
    pruned: list[tuple[str, float]] = []
    _wire(
        monkeypatch, pruned, removed=1,
        rows={"code__1-1__m__v1": "code", "code__1-1__m__v10": "knowledge"},
    )
    result = CliRunner().invoke(
        taxonomy, ["project", "code__1-1__m__v1", "--prune-below-threshold"],
    )
    assert result.exit_code == 0, result.output
    assert pruned == [("code__1-1__m__v1", 0.5)]
    assert "1 skipped" in result.output


def test_prune_single_collection_without_extensions_prunes_at_its_own_threshold(monkeypatch) -> None:
    pruned: list[tuple[str, float]] = []
    _wire(monkeypatch, pruned, removed=1, rows={"code__a": "code", "knowledge__b": "knowledge"})
    result = CliRunner().invoke(taxonomy, ["project", "code__a", "--prune-below-threshold"])
    assert result.exit_code == 0, result.output
    assert pruned == [("code__a", 0.7)]
    assert "skipped" not in result.output


def test_prune_does_not_skip_a_prefix_name_whose_threshold_is_not_higher(monkeypatch) -> None:
    pruned: list[tuple[str, float]] = []
    _wire(
        monkeypatch, pruned, removed=1,
        rows={"knowledge__x": "knowledge", "knowledge__x2": "code"},
    )
    result = CliRunner().invoke(taxonomy, ["project", "--backfill", "--prune-below-threshold"])
    assert result.exit_code == 0, result.output
    assert pruned == [("knowledge__x", 0.5), ("knowledge__x2", 0.7)]


def test_prune_single_collection_default_threshold_reads_the_row(monkeypatch) -> None:
    pruned: list[tuple[str, float]] = []
    name = "code__1-61__bge-base-en-v15-768__v1"
    _wire(monkeypatch, pruned, removed=1, rows={name: "knowledge"})
    result = CliRunner().invoke(taxonomy, ["project", name, "--prune-below-threshold"])
    assert result.exit_code == 0, result.output
    assert pruned == [(name, 0.5)]


def test_prune_single_collection_uses_the_explicit_threshold(monkeypatch) -> None:
    pruned: list[tuple[str, float]] = []
    _wire(monkeypatch, pruned, removed=1)
    result = CliRunner().invoke(
        taxonomy,
        ["project", "code__1-61__bge-base-en-v15-768__v1", "--prune-below-threshold", "--threshold", "0.8"],
    )
    assert result.exit_code == 0, result.output
    assert pruned == [("code__1-61__bge-base-en-v15-768__v1", 0.8)]


@pytest.mark.parametrize(
    "rows, skipped",
    [
        # The lowest extension comes FIRST in name order, a higher one after it: a plan that lets
        # the last hit under the collection's own threshold win (`< thr`) ends at 0.55, not 0.50.
        ({"code__1-1__m__v1": "code", "code__1-1__m__v10": "knowledge", "code__1-1__m__v100": "docs"}, 1),
        # The lowest extension comes LAST: a plan that stops at the first hit ends at 0.55, not 0.50.
        # (v10 is a skip too here: its own 0.55 is above v100's 0.50.)
        ({"code__2-2__m__v1": "code", "code__2-2__m__v10": "docs", "code__2-2__m__v100": "knowledge"}, 2),
    ],
    ids=["lowest-extension-first", "lowest-extension-last"],
)
def test_prune_a_prefix_chain_of_three_takes_the_first_name_to_the_lowest_threshold(monkeypatch, rows, skipped) -> None:
    """``v1`` prefixes ``v10`` which prefixes ``v100``: ``v1`` goes to the minimum over BOTH
    extensions, ``v10`` to the minimum over its own one, and only ``v1`` is a skip."""
    pruned: list[tuple[str, float]] = []
    _wire(monkeypatch, pruned, removed=1, rows=rows)
    result = CliRunner().invoke(taxonomy, ["project", "--backfill", "--prune-below-threshold"])
    assert result.exit_code == 0, result.output
    v1, v10, v100 = sorted(rows)
    thresholds = {"code": 0.7, "docs": 0.55, "knowledge": 0.5}
    assert pruned == [
        (v1, 0.5), (v10, min(thresholds[rows[v10]], thresholds[rows[v100]])), (v100, thresholds[rows[v100]]),
    ]
    assert f"{skipped} skipped" in result.output


def test_prune_refuses_a_name_when_an_extension_row_is_unreadable(monkeypatch) -> None:
    """The floor of ``v1`` would be taken from ``v10``'s guessed threshold: refuse both, prune the rest,
    and exit non-zero because the recovery is incomplete."""
    pruned: list[tuple[str, float]] = []
    _wire(
        monkeypatch, pruned, removed=1, unreadable_names=("code__1-1__m__v10",),
        rows={"code__1-1__m__v1": "code", "code__1-1__m__v10": "knowledge", "docs__z": "docs"},
    )
    result = CliRunner().invoke(taxonomy, ["project", "--backfill", "--prune-below-threshold"])
    assert pruned == [("docs__z", 0.55)]
    assert "code__1-1__m__v1: refused" in result.output
    assert "code__1-1__m__v10: refused" in result.output
    assert "2 refused" in result.output
    assert result.exit_code != 0, "any refused collection means the recovery is incomplete"


def test_prune_sweep_with_some_refused_exits_nonzero_and_a_clean_sweep_exits_zero(monkeypatch) -> None:
    pruned: list[tuple[str, float]] = []
    _wire(
        monkeypatch, pruned, removed=1, unreadable_names=("knowledge__b",),
        rows={"code__a": "code", "knowledge__b": "knowledge"},
    )
    result = CliRunner().invoke(taxonomy, ["project", "--backfill", "--prune-below-threshold"])
    assert pruned == [("code__a", 0.7)]
    assert "1 refused" in result.output
    assert result.exit_code != 0
    pruned.clear()
    _wire(monkeypatch, pruned, removed=1, rows={"code__a": "code", "knowledge__b": "knowledge"})
    clean = CliRunner().invoke(taxonomy, ["project", "--backfill", "--prune-below-threshold"])
    assert clean.exit_code == 0, clean.output
    assert "refused" not in clean.output


def test_prune_plan_reaches_collections_only_the_projection_rows_name(monkeypatch) -> None:
    """A collection with projection rows but no topics and no live row is in neither listing, yet the engine's
    prefix match reaches its rows from a shorter name: the plan takes the distinct ``source_collection``
    values of the projection rows into its universe (review r2 S-e)."""
    pruned: list[tuple[str, float]] = []
    _wire(
        monkeypatch, pruned, removed=1, rows={"knowledge__a": "code"},
        projection_counts={"knowledge__a": 4, "knowledge__a2": 3},
    )
    result = CliRunner().invoke(taxonomy, ["project", "--backfill", "--prune-below-threshold"])
    assert result.exit_code == 0, result.output
    # knowledge__a2 has no row: the name says knowledge, 0.50, below knowledge__a's own 0.70.
    assert pruned == [("knowledge__a", 0.5), ("knowledge__a2", 0.5)]
    assert "1 skipped" in result.output


# ── the engine's exact-name prune mode (linda's ruling on nexus-p2m1x.9) ─────


def test_prune_sends_exact_and_drops_the_collision_guard_when_the_engine_supports_it(monkeypatch) -> None:
    pruned: list[tuple[str, float]] = []
    exact_log: list[bool] = []
    _wire(
        monkeypatch, pruned, removed=1, exact=True, exact_log=exact_log,
        rows={"code__1-1__m__v1": "code", "code__1-1__m__v10": "knowledge"},
    )
    result = CliRunner().invoke(taxonomy, ["project", "--backfill", "--prune-below-threshold"])
    assert result.exit_code == 0, result.output
    assert pruned == [("code__1-1__m__v1", 0.7), ("code__1-1__m__v10", 0.5)], "each at its OWN threshold"
    assert exact_log == [True, True]
    assert "skipped" not in result.output


@pytest.mark.parametrize("echo", [None, "prefix"], ids=["no-echo", "echoed-prefix"])
def test_prune_stops_at_once_when_the_engine_did_not_apply_exact_matching(monkeypatch, echo) -> None:
    """The instance can change after the verification prune confirmed exact (the second line of defence): a
    real reply whose ``match`` is not ``exact`` ends the sweep after the FIRST real prune, exits non-zero,
    and says so."""
    pruned: list[tuple[str, float]] = []
    exact_log: list[bool] = []
    _wire(
        monkeypatch, pruned, removed=1, exact=True, exact_log=exact_log, echo=echo, probe_echo="exact",
        rows={"code__1-1__m__v1": "code", "code__1-1__m__v10": "knowledge", "docs__z": "docs"},
    )
    result = CliRunner().invoke(taxonomy, ["project", "--backfill", "--prune-below-threshold"])
    assert len(pruned) == 1, "never continue exact-planned prunes on an engine that did not echo exact"
    assert exact_log == [True]
    assert "the engine did not apply exact matching; no further collections pruned" in result.output
    assert "rerun without exact or after the engine is current" in result.output
    assert pruned[0][0] in result.output, "names the collection that already ran in the engine's own mode"
    assert result.exit_code != 0


def test_a_verification_prune_precedes_the_first_real_exact_prune(monkeypatch) -> None:
    """Before any real prune with exact=True, one prune with match=exact goes to a name no collection has (a
    fixed prefix and a random 128-bit suffix): even a prefix match reaches no rows, so an engine that ignores
    exact is found out without a real collection being touched. The guarantee is the name, not the threshold."""
    pruned: list[tuple[str, float]] = []
    probes: list = []
    rows = {"code__1-1__m__v1": "code", "code__1-1__m__v10": "knowledge"}
    _wire(monkeypatch, pruned, removed=1, exact=True, probe_log=probes, rows=rows)
    result = CliRunner().invoke(taxonomy, ["project", "--backfill", "--prune-below-threshold"])
    assert result.exit_code == 0, result.output
    assert len(probes) == 1
    name, thr, exact_sent, real_before = probes[0]
    assert name.startswith("nexus-exact-probe-") and name not in rows
    assert not any(real.startswith(name) or name.startswith(real) for real in rows), "matches no collection"
    assert (thr, exact_sent) == (0.0, True)
    assert real_before == [], "before the first real prune"
    assert len(pruned) == 2 and "did not confirm" not in result.output


@pytest.mark.parametrize("probe_echo", [None, "prefix"], ids=["no-echo", "echoed-prefix"])
def test_an_unconfirmed_verification_falls_back_to_the_guarded_plan_for_the_whole_sweep(
    monkeypatch, probe_echo,
) -> None:
    pruned: list[tuple[str, float]] = []
    exact_log: list[bool] = []
    _wire(
        monkeypatch, pruned, removed=1, exact=True, exact_log=exact_log, probe_echo=probe_echo, echo="auto",
        rows={"code__1-1__m__v1": "code", "code__1-1__m__v10": "knowledge", "docs__z": "docs"},
    )
    result = CliRunner().invoke(taxonomy, ["project", "--backfill", "--prune-below-threshold"])
    assert result.exit_code == 0, result.output
    assert "the engine did not confirm exact matching; using the guarded prune" in result.output
    assert exact_log == [False, False, False], "nothing is pruned in exact mode"
    assert pruned == [("code__1-1__m__v1", 0.5), ("code__1-1__m__v10", 0.5), ("docs__z", 0.55)], "the floor applies"
    assert "1 skipped" in result.output


def test_a_verification_prune_that_fails_falls_back_to_the_guarded_plan(monkeypatch) -> None:
    pruned: list[tuple[str, float]] = []
    exact_log: list[bool] = []
    _wire(
        monkeypatch, pruned, removed=1, exact=True, exact_log=exact_log, probe_raises=True,
        rows={"code__1-1__m__v1": "code", "code__1-1__m__v10": "knowledge"},
    )
    result = CliRunner().invoke(taxonomy, ["project", "--backfill", "--prune-below-threshold"])
    assert result.exit_code == 0, result.output
    assert "using the guarded prune" in result.output
    assert exact_log == [False, False] and len(pruned) == 2


def test_the_single_source_form_is_verified_too(monkeypatch) -> None:
    pruned: list[tuple[str, float]] = []
    exact_log: list[bool] = []
    probes: list = []
    _wire(
        monkeypatch, pruned, removed=1, exact=True, exact_log=exact_log, probe_log=probes, probe_echo=None,
        rows={"code__1-1__m__v1": "code", "code__1-1__m__v10": "knowledge"},
    )
    result = CliRunner().invoke(taxonomy, ["project", "code__1-1__m__v1", "--prune-below-threshold"])
    assert result.exit_code == 0, result.output
    assert len(probes) == 1 and exact_log == [False]
    assert pruned == [("code__1-1__m__v1", 0.5)], "the guarded floor, not the exact threshold"


def test_prune_continues_when_the_engine_echoes_exact(monkeypatch) -> None:
    pruned: list[tuple[str, float]] = []
    _wire(
        monkeypatch, pruned, removed=1, exact=True, echo="exact",
        rows={"code__1-1__m__v1": "code", "code__1-1__m__v10": "knowledge", "docs__z": "docs"},
    )
    result = CliRunner().invoke(taxonomy, ["project", "--backfill", "--prune-below-threshold"])
    assert result.exit_code == 0, result.output
    assert len(pruned) == 3
    assert "did not apply" not in result.output


def test_prune_without_exact_does_not_read_the_echo(monkeypatch) -> None:
    """In the prefix mode an engine that predates the echo returns no ``match``: that is the normal case."""
    pruned: list[tuple[str, float]] = []
    _wire(monkeypatch, pruned, removed=1, exact=False, echo=None, rows={"code__a": "code", "docs__z": "docs"})
    result = CliRunner().invoke(taxonomy, ["project", "--backfill", "--prune-below-threshold"])
    assert result.exit_code == 0, result.output
    assert len(pruned) == 2


def test_prune_single_collection_sends_exact_when_the_engine_supports_it(monkeypatch) -> None:
    pruned: list[tuple[str, float]] = []
    exact_log: list[bool] = []
    _wire(
        monkeypatch, pruned, removed=1, exact=True, exact_log=exact_log,
        rows={"code__1-1__m__v1": "code", "code__1-1__m__v10": "knowledge"},
    )
    result = CliRunner().invoke(taxonomy, ["project", "code__1-1__m__v1", "--prune-below-threshold"])
    assert result.exit_code == 0, result.output
    assert pruned == [("code__1-1__m__v1", 0.7)]
    assert exact_log == [True]


def test_prune_keeps_the_guard_and_the_prefix_mode_against_an_engine_without_exact(monkeypatch) -> None:
    pruned: list[tuple[str, float]] = []
    exact_log: list[bool] = []
    _wire(
        monkeypatch, pruned, removed=1, exact=False, exact_log=exact_log,
        rows={"code__1-1__m__v1": "code", "code__1-1__m__v10": "knowledge"},
    )
    result = CliRunner().invoke(taxonomy, ["project", "--backfill", "--prune-below-threshold"])
    assert result.exit_code == 0, result.output
    assert exact_log == [False, False]
    assert "1 skipped" in result.output


class _Reader:
    """A catalog reader whose ``supports_taxonomy_prune_exact`` answers *answer*, or raises it."""

    def __init__(self, answer) -> None:
        self.answer = answer
        self.asked = 0

    def supports_taxonomy_prune_exact(self) -> bool:
        self.asked += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _use_reader(monkeypatch, reader) -> None:
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda **_kw: reader)


def test_prune_supports_exact_reads_the_version_flag_through_the_catalog_reader(monkeypatch) -> None:
    from nexus.commands.taxonomy_cmd import prune_supports_exact

    _use_reader(monkeypatch, _Reader(True))
    assert prune_supports_exact() is True
    for not_true in (False, None, RuntimeError("version unreadable")):
        _use_reader(monkeypatch, _Reader(not_true))
        assert prune_supports_exact() is False, repr(not_true)


def test_prune_supports_exact_is_false_when_the_reader_has_no_probe(monkeypatch) -> None:
    from nexus.commands.taxonomy_cmd import prune_supports_exact

    _use_reader(monkeypatch, object())
    assert prune_supports_exact() is False


@pytest.mark.parametrize("answer", [False, RuntimeError("version unreadable")], ids=["absent-or-false", "unreadable"])
def test_prune_keeps_the_guard_when_the_version_flag_is_absent_false_or_unreadable(monkeypatch, answer) -> None:
    pruned: list[tuple[str, float]] = []
    exact_log: list[bool] = []
    _wire(
        monkeypatch, pruned, removed=1, exact_log=exact_log, probe=False,
        rows={"code__1-1__m__v1": "code", "code__1-1__m__v10": "knowledge"},
    )
    _use_reader(monkeypatch, _Reader(answer))
    result = CliRunner().invoke(taxonomy, ["project", "--backfill", "--prune-below-threshold"])
    assert result.exit_code == 0, result.output
    assert exact_log == [False, False]
    assert pruned == [("code__1-1__m__v1", 0.5), ("code__1-1__m__v10", 0.5)]
    assert "1 skipped" in result.output


def test_prune_sends_match_exact_with_no_floor_when_the_version_flag_is_true_and_probes_once(monkeypatch) -> None:
    pruned: list[tuple[str, float]] = []
    exact_log: list[bool] = []
    _wire(
        monkeypatch, pruned, removed=1, exact_log=exact_log, probe=False,
        rows={"code__1-1__m__v1": "code", "code__1-1__m__v10": "knowledge", "docs__z": "docs"},
    )
    reader = _Reader(True)
    _use_reader(monkeypatch, reader)
    result = CliRunner().invoke(taxonomy, ["project", "--backfill", "--prune-below-threshold"])
    assert result.exit_code == 0, result.output
    assert pruned == [("code__1-1__m__v1", 0.7), ("code__1-1__m__v10", 0.5), ("docs__z", 0.55)]
    assert exact_log == [True, True, True]
    assert "skipped" not in result.output
    assert reader.asked == 1, "one probe per command, never one per collection"
