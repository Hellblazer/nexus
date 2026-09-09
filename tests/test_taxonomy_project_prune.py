"""GH #1528 (nexus-4tfxp): ``nx taxonomy project --prune-below-threshold`` is
the recovery path for a projection pass that admitted weak matches: one
engine-side delete per source-collection prefix, on the stored raw cosine,
at that prefix's threshold."""

from __future__ import annotations

from click.testing import CliRunner

from nexus.commands.taxonomy_cmd import taxonomy


def _wire(monkeypatch, pruned: list[tuple[str, float]], removed: int) -> None:
    class _Taxonomy:
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


def test_prune_with_backfill_walks_every_prefix_at_its_default_threshold(monkeypatch) -> None:
    pruned: list[tuple[str, float]] = []
    _wire(monkeypatch, pruned, removed=5)
    result = CliRunner().invoke(taxonomy, ["project", "--backfill", "--prune-below-threshold"])
    assert result.exit_code == 0, result.output
    assert pruned == [("code__", 0.7), ("knowledge__", 0.5), ("docs__", 0.55), ("rdr__", 0.55)]
    assert "Pruned 20 projection assignment(s)." in result.output


def test_prune_single_collection_uses_the_explicit_threshold(monkeypatch) -> None:
    pruned: list[tuple[str, float]] = []
    _wire(monkeypatch, pruned, removed=1)
    result = CliRunner().invoke(
        taxonomy,
        ["project", "code__1-61__bge-base-en-v15-768__v1", "--prune-below-threshold", "--threshold", "0.8"],
    )
    assert result.exit_code == 0, result.output
    assert pruned == [("code__1-61__bge-base-en-v15-768__v1", 0.8)]
