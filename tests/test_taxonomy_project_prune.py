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
) -> None:
    """*rows* maps collection name -> catalog content_type (the live listing)."""
    rows = rows or {}

    class _Taxonomy:
        def get_distinct_collections(self) -> list[str]:
            return list(distinct or [])

        def prune_projection_below(self, prefix: str, thr: float) -> int:
            pruned.append((prefix, round(thr, 2)))
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
    monkeypatch.setattr("nexus.mcp_infra.get_collection_names", lambda: sorted(rows))
    monkeypatch.setattr(
        "nexus.mcp_infra.get_collection_row",
        lambda name, *, refresh=True, strict=False: (
            {"content_type": rows[name]} if name in rows else None
        ),
    )


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

    monkeypatch.setattr("nexus.mcp_infra.get_collection_names", _boom)
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


def test_prune_skips_a_name_that_prefixes_a_collection_with_a_lower_threshold(monkeypatch) -> None:
    """The engine prunes by name prefix: ``code__1-1__m__v1`` at 0.70 would also delete the
    rows of ``code__1-1__m__v10`` (a knowledge row, 0.50) between 0.50 and 0.70."""
    pruned: list[tuple[str, float]] = []
    _wire(
        monkeypatch, pruned, removed=1,
        rows={"code__1-1__m__v1": "code", "code__1-1__m__v10": "knowledge"},
    )
    result = CliRunner().invoke(taxonomy, ["project", "--backfill", "--prune-below-threshold"])
    assert result.exit_code == 0, result.output
    assert pruned == [("code__1-1__m__v10", 0.5)]
    assert "code__1-1__m__v1: skipped" in result.output


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
