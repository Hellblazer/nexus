"""GH #1528 (nexus-4tfxp): ``nx taxonomy project --prune-below-threshold`` is
the recovery path for a projection pass that admitted weak matches: one
engine-side delete per source collection, on the stored raw cosine, at that
collection's threshold (RDR-229 P2.4: its catalog row's content type, so the
walk is per full collection name and never per name prefix)."""

from __future__ import annotations

from click.testing import CliRunner

from nexus.commands.taxonomy_cmd import taxonomy


def _wire(
    monkeypatch, pruned: list[tuple[str, float]], removed: int,
    *, rows: dict[str, str] | None = None, distinct: list[str] | None = None,
    unreadable: bool = False, exact: bool = False, exact_log: list[bool] | None = None,
) -> None:
    """*rows* maps collection name -> catalog content_type (the live listing).
    *unreadable* makes the listing and every row read raise, as an outage does.
    *exact* is what the engine's exact-name prune capability probe answers; *exact_log*
    collects the ``exact`` flag of each prune call."""
    rows = rows or {}
    monkeypatch.setattr("nexus.commands.taxonomy_cmd.prune_supports_exact", lambda: exact, raising=False)

    class _Taxonomy:
        def get_distinct_collections(self) -> list[str]:
            return list(distinct or [])

        def prune_projection_below(self, prefix: str, thr: float, *, exact: bool = False) -> int:
            pruned.append((prefix, round(thr, 2)))
            if exact_log is not None:
                exact_log.append(exact)
            return removed

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
        if unreadable:
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


def test_prune_supports_exact_is_false_until_the_engine_flag_is_known() -> None:
    from nexus.commands.taxonomy_cmd import prune_supports_exact

    assert prune_supports_exact() is False
