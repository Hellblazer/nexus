# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-7f4o4: a rename must not leave a conformant name whose model token
disagrees with the source collection's catalog row.

The engine rename keeps the SOURCE row's ``embedding_model`` (the client sends
only content_type/owner_id), so the vectors stay in the row's model space
whatever token the new name carries. Three sites used to ignore the row:

  - ``nx collection rename``: compared name-derived models, so a legacy
    bge-row ``docs__foo`` (reads as voyage by name) renamed to a voyage token
    passed and the correct bge rename was refused;
  - ``nx catalog rename-collection``: checked only that the new name is
    conformant; it now compares against the engine row it already fetches
    (``get_collection``), not the stats-view cache;
  - the indexer Phase-4 migration: a legacy collection whose row model
    differs from the write model is kept under its legacy name on every run.
    Renaming it to the row's token flips the target on the next run (the
    steady state mints from the write model) and strands the data; renaming
    it to the write model's token claims vectors it does not hold.

Engine-less: every test patches ``nexus.mcp_infra.get_collection_row`` (the
row source ``resolve_row_preferred`` reads) and the data plane, so nothing
reaches storage.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.catalog.collection_name import CollectionName, owner_segment_for_tumbler
from nexus.corpus import rename_model_conflict

pytestmark = pytest.mark.usefixtures("cloud_mode")

_BGE = "bge-base-en-v15-768"
_VOYAGE = "voyage-context-3"
_COUNTS = {
    "tax_topics": 0, "tax_assignments": 0, "tax_meta": 0,
    "chash": 0, "catalog_docs": 0,
}


def _row(model: str, content_type: str = "docs") -> dict:
    return {
        "content_type": content_type, "owner_id": "legacy",
        "embedding_model": model, "lifecycle_state": "live",
    }


def _rows(mapping: dict[str, dict]):
    return lambda name, **_kw: mapping.get(name)


# ── nx collection rename ────────────────────────────────────────────────────


def _invoke_collection_rename(old: str, new: str, rows: dict, *extra: str):
    from nexus.commands.collection import rename_cmd

    with patch(
        "nexus.commands.collection.rename_collection_data_plane",
        return_value=_COUNTS,
    ) as cascade, patch(
        "nexus.mcp_infra.get_collection_row", side_effect=_rows(rows),
    ):
        result = CliRunner().invoke(rename_cmd, [old, new, *extra])
    return result, cascade


class TestCollectionRenameVerb:
    def test_bge_row_renamed_to_voyage_token_is_refused(self) -> None:
        old = "docs__foo"
        new = f"docs__1-1__{_VOYAGE}__v1"
        result, cascade = _invoke_collection_rename(
            old, new, {old: _row(_BGE)},
        )
        assert result.exit_code != 0, result.output
        out = result.output
        assert _BGE in out and _VOYAGE in out
        # The refusal names the rename that WOULD be accepted.
        assert f"docs__1-1__{_BGE}__v1" in out
        cascade.assert_not_called()

    def test_bge_row_renamed_to_bge_token_succeeds(self) -> None:
        old = "docs__foo"
        new = f"docs__1-1__{_BGE}__v1"
        result, cascade = _invoke_collection_rename(
            old, new, {old: _row(_BGE)},
        )
        assert result.exit_code == 0, result.output
        cascade.assert_called_once_with(old, new)

    def test_force_flag_still_bypasses(self) -> None:
        old = "docs__foo"
        new = f"docs__1-1__{_VOYAGE}__v1"
        result, cascade = _invoke_collection_rename(
            old, new, {old: _row(_BGE)}, "--force-prefix-change",
        )
        assert result.exit_code == 0, result.output
        cascade.assert_called_once_with(old, new)

    def test_row_beats_the_old_names_own_token(self) -> None:
        """A conformant old name whose row disagrees with its own token: the
        row wins, so renaming to the NAME's token (the old guard's answer) is
        the refused one."""
        old = f"docs__1-1__{_VOYAGE}__v1"
        new = f"docs__2-1__{_VOYAGE}__v1"
        result, cascade = _invoke_collection_rename(
            old, new, {old: _row(_BGE)},
        )
        assert result.exit_code != 0, result.output
        assert f"docs__2-1__{_BGE}__v1" in result.output
        cascade.assert_not_called()

    def test_no_row_legacy_falls_back_to_name_derivation_as_today(self) -> None:
        """No row backs a legacy `docs__foo`: it reads as voyage by name, so a
        voyage token is accepted and a bge token needs the force flag, exactly
        as before the row was consulted."""
        old = "docs__foo"
        ok, cascade_ok = _invoke_collection_rename(
            old, f"docs__1-1__{_VOYAGE}__v1", {},
        )
        assert ok.exit_code == 0, ok.output
        cascade_ok.assert_called_once()

        refused, cascade_no = _invoke_collection_rename(
            old, f"docs__1-1__{_BGE}__v1", {},
        )
        assert refused.exit_code != 0, refused.output
        assert "embedding-model mismatch" in refused.output
        cascade_no.assert_not_called()

    def test_non_conformant_target_keeps_name_vs_name_comparison(self) -> None:
        """A row cannot disagree with a token the new name does not carry:
        legacy -> legacy within one prefix is accepted whatever the row says."""
        old = "docs__foo"
        result, cascade = _invoke_collection_rename(
            old, "docs__bar", {old: _row(_BGE)},
        )
        assert result.exit_code == 0, result.output
        cascade.assert_called_once_with(old, "docs__bar")


class TestRenameModelConflictHelper:
    def test_empty_row_model_gives_no_basis_to_refuse(self) -> None:
        with patch(
            "nexus.mcp_infra.get_collection_row",
            side_effect=_rows({"docs__foo": _row("")}),
        ):
            assert rename_model_conflict(
                "docs__foo", f"docs__1-1__{_BGE}__v1",
            ) is None

    def test_name_fallback_hook_can_decline_the_legacy_guess(self) -> None:
        from nexus.corpus import embedding_model_for_collection_name

        with patch("nexus.mcp_infra.get_collection_row", return_value=None):
            assert rename_model_conflict(
                "docs__foo", f"docs__1-1__{_BGE}__v1",
            ) is not None  # default: the voyage guess
            assert rename_model_conflict(
                "docs__foo", f"docs__1-1__{_BGE}__v1",
                name_fallback=embedding_model_for_collection_name,
            ) is None  # no row, no conformant token: no basis


# ── nx catalog rename-collection ────────────────────────────────────────────
#
# Engine-less: the verb's catalog, writer and T3 handles are replaced, so the
# only things under test are the verb's own gates. The model comes from the
# engine row the verb ALREADY fetched with get_collection, never from the
# stats-view cache (`mcp_infra.get_collection_row`).


class _FakeVerbCatalog:
    def __init__(self, rows: dict[str, dict]) -> None:
        self._rows = rows

    def get_collection(self, name: str):
        return self._rows.get(name)


def _engine_row(model: str | None, **extra) -> dict:
    row = {"name": "docs__foo", "content_type": "docs", "owner_id": "legacy",
           "superseded_by": None}
    if model is not None:
        row["embedding_model"] = model
    row.update(extra)
    return row


def _invoke_catalog_verb(
    old: str, new: str, old_row: dict | None, *, stats_row=None,
    stats_raises: bool = False,
):
    """Run `nx catalog rename-collection <old> <new> --yes`; returns
    (result, data_plane_mock)."""
    from nexus.cli import main

    cat = _FakeVerbCatalog({old: old_row} if old_row is not None else {})
    if stats_raises:
        stats = patch(
            "nexus.mcp_infra.get_collection_row",
            side_effect=RuntimeError("stats listing failed"),
        )
    else:
        stats = patch("nexus.mcp_infra.get_collection_row", return_value=stats_row)
    writer = MagicMock()
    writer.supersede_collection.return_value = 1
    with patch("nexus.commands.catalog._get_catalog", return_value=cat), patch(
        "nexus.commands.catalog._get_catalog_writer", return_value=writer,
    ), patch("nexus.corpus.ensure_collection_registered"), patch(
        "nexus.db.make_t3", return_value=_FakeT3(old),
    ), patch(
        "nexus.commands.collection.rename_collection_data_plane",
        return_value=_COUNTS,
    ) as data_plane, stats:
        result = CliRunner().invoke(
            main, ["catalog", "rename-collection", old, new, "--yes"],
        )
    return result, data_plane


class TestCatalogVerbRowGuard:
    def test_bge_row_renamed_to_voyage_token_is_refused(self) -> None:
        old = "docs__foo"
        new = f"docs__1-1__{_VOYAGE}__v1"
        result, data_plane = _invoke_catalog_verb(old, new, _engine_row(_BGE))
        assert result.exit_code != 0, result.output
        out = result.output
        assert _BGE in out and _VOYAGE in out
        assert f"docs__1-1__{_BGE}__v1" in out  # the rename that would pass
        assert "--force-prefix-change" in out  # the named override
        data_plane.assert_not_called()

    def test_bge_row_renamed_to_its_own_token_succeeds(self) -> None:
        old = "docs__foo"
        result, data_plane = _invoke_catalog_verb(
            old, f"docs__1-1__{_BGE}__v1", _engine_row(_BGE),
        )
        assert result.exit_code == 0, result.output
        data_plane.assert_called_once()

    def test_a_failed_stats_listing_cannot_let_a_mismatch_through(self) -> None:
        """The model is the engine row's. With the stats-view cache raising,
        the old cache-backed read fell to the name (voyage by derivation for a
        legacy name), so a voyage token slipped through for a bge row."""
        old = "docs__foo"
        new = f"docs__1-1__{_VOYAGE}__v1"
        result, data_plane = _invoke_catalog_verb(
            old, new, _engine_row(_BGE), stats_raises=True,
        )
        assert result.exit_code != 0, result.output
        assert _BGE in result.output
        data_plane.assert_not_called()

    def test_a_stats_row_cannot_override_the_engine_row(self) -> None:
        """The cache says voyage, the engine row says bge: the engine row wins."""
        old = "docs__foo"
        new = f"docs__1-1__{_VOYAGE}__v1"
        result, data_plane = _invoke_catalog_verb(
            old, new, _engine_row(_BGE),
            stats_row=_row(_VOYAGE) | {"lifecycle_state": "live"},
        )
        assert result.exit_code != 0, result.output
        data_plane.assert_not_called()

    def test_legacy_row_without_a_model_has_no_basis_to_refuse(self) -> None:
        old = "docs__foo"
        result, data_plane = _invoke_catalog_verb(
            old, f"docs__1-1__{_VOYAGE}__v1", _engine_row(None),
        )
        assert result.exit_code == 0, result.output
        data_plane.assert_called_once()

    def test_disputed_source_skips_the_comparison_with_a_warning(self) -> None:
        """hygiene-002 fallback rows are `disputed`: the row model is itself in
        doubt, so the mismatch is not enforced. The single-row read carries no
        lifecycle_state, so it comes from the stats cache."""
        old = "docs__foo"
        new = f"docs__1-1__{_VOYAGE}__v1"
        result, data_plane = _invoke_catalog_verb(
            old, new, _engine_row(_BGE),
            stats_row=_row(_BGE) | {"lifecycle_state": "disputed"},
        )
        assert result.exit_code == 0, result.output
        assert "disputed" in result.output
        data_plane.assert_called_once()

    def test_disputed_state_on_the_engine_row_itself_also_skips(self) -> None:
        old = "docs__foo"
        result, data_plane = _invoke_catalog_verb(
            old, f"docs__1-1__{_VOYAGE}__v1",
            _engine_row(_BGE, lifecycle_state="disputed"),
        )
        assert result.exit_code == 0, result.output
        assert "disputed" in result.output
        data_plane.assert_called_once()


def test_collection_rename_verb_skips_a_disputed_source_with_a_warning() -> None:
    old = "docs__foo"
    result, cascade = _invoke_collection_rename(
        old, f"docs__1-1__{_VOYAGE}__v1",
        {old: _row(_BGE) | {"lifecycle_state": "disputed"}},
    )
    assert result.exit_code == 0, result.output
    assert "disputed" in result.output
    cascade.assert_called_once()


# ── indexer Phase-4 migration ───────────────────────────────────────────────


class _FakeCat:
    """owner_for_repo + collection_for, rendering through the real
    CollectionName so the minted name is the production shape."""

    def owner_for_repo(self, repo_hash: str) -> str:
        return "1.1"

    def collection_for(self, content_type, owner, embedding_model, *, bump=False):
        return CollectionName(
            content_type=content_type,
            owner_id=owner_segment_for_tumbler(owner),
            embedding_model=embedding_model,
            model_version=1,
        )


class _RowCat(_FakeCat):
    """A catalog that answers get_collection with a row naming *model*."""

    def __init__(self, model: str) -> None:
        self.model = model

    def get_collection(self, name: str) -> dict:
        return {"name": name, "embedding_model": self.model}


class _FailingRowCat(_FakeCat):
    def get_collection(self, name: str) -> dict:
        raise RuntimeError("catalog unreachable")


class _FakeT3:
    def __init__(self, *names: str) -> None:
        self.names = set(names)

    def collection_exists(self, name: str) -> bool:
        return name in self.names


_CODE_WRITE = "voyage-code-3"  # cloud_mode write model for `code`
_CODE_TOKEN = f"code__1-1__{_CODE_WRITE}__v1"


class _MigrationRig:
    """One repo, one fake engine: runs `_migrate_legacy_collections` as often
    as asked against shared T3 / row state, and applies the engine's rename
    (legacy gone, target present with the SOURCE row's model) between runs on
    request. Every run's result and log events are kept."""

    def __init__(self, tmp_path: Path, monkeypatch, *, row_model: str | None,
                 extra_t3: tuple[str, ...] = ()) -> None:
        from nexus.indexer import _legacy_collection_name

        self.repo = tmp_path / "myproject"
        self.repo.mkdir()
        monkeypatch.setattr(
            "nexus.repo_identity._repo_identity",
            lambda r: ("myproject", "cafef00d"),
        )
        self.legacy = _legacy_collection_name(self.repo, "code")
        self.rows: dict[str, dict] = (
            {} if row_model is None else {self.legacy: _row(row_model, "code")}
        )
        self.t3 = _FakeT3(self.legacy, *extra_t3)
        self.cat: _FakeCat = _FakeCat()
        self.results: list[dict[str, str]] = []
        self.total_renames = 0
        self.events: list[list[dict]] = []

    def run(self) -> str:
        from structlog.testing import capture_logs

        from nexus.indexer import _migrate_legacy_collections

        with patch(
            "nexus.mcp_infra.get_collection_row", side_effect=_rows(self.rows),
        ), patch(
            "nexus.collection_rename.rename_collection_data_plane",
            return_value=_COUNTS,
        ) as self.rename, patch("nexus.corpus.ensure_collection_registered"), \
                capture_logs() as cap:
            result = _migrate_legacy_collections(
                self.repo, cat=self.cat, t3_db=self.t3, registry=MagicMock(),
                writer=MagicMock(),
            )
        self.total_renames += self.rename.call_count
        self.results.append(result)
        self.events.append(list(cap))
        return result["code"]

    def engine_renames_to(self, target: str) -> None:
        """What the engine does after a successful rename call: the legacy
        name is gone and the target carries the SOURCE row's model."""
        self.t3.names.discard(self.legacy)
        self.t3.names.add(target)
        self.rows[target] = self.rows.pop(self.legacy)

    def warnings(self, run: int) -> list[dict]:
        return [
            e for e in self.events[run]
            if e.get("event") == "phase4_migration_row_model_differs_from_write_model"
        ]


class TestIndexerMigrationKeepsAMismodelledLegacyCollection:
    """The engine rename keeps the legacy row's model. Minting the target
    from the row flips it on the next run (the steady-state path mints from
    the write model) and strands the migrated data; renaming to the write
    model's token claims vectors the collection does not hold. A legacy
    collection whose row model differs from the write model therefore stays
    where it is, on every run."""

    def test_differing_row_keeps_the_legacy_name_on_every_run(
        self, tmp_path, monkeypatch,
    ) -> None:
        rig = _MigrationRig(tmp_path, monkeypatch, row_model=_BGE)
        runs = [rig.run(), rig.run(), rig.run()]
        assert runs == [rig.legacy] * 3
        assert rig.total_renames == 0
        # One warning per run, naming both models and the exit.
        for i in range(3):
            (warning,) = rig.warnings(i)
            assert warning["row_model"] == _BGE
            assert warning["write_model"] == _CODE_WRITE
            assert warning["legacy"] == rig.legacy
            assert "re-embed" in warning["reason"]

    def test_row_model_a_name_cannot_carry_is_kept_the_same_way(
        self, tmp_path, monkeypatch,
    ) -> None:
        """A pre-RDR-103 `voyage-3` row is not a canonical token at all."""
        rig = _MigrationRig(tmp_path, monkeypatch, row_model="voyage-3")
        assert [rig.run(), rig.run()] == [rig.legacy] * 2
        assert rig.total_renames == 0
        assert rig.warnings(0)[0]["row_model"] == "voyage-3"

    def test_partial_state_with_a_write_model_sibling_is_stable_and_not_renamed(
        self, tmp_path, monkeypatch,
    ) -> None:
        """Legacy bge row AND an existing write-model sibling: case 3 as on
        develop (the sibling is the target, nothing is renamed), and the
        answer does not flip between runs."""
        rig = _MigrationRig(
            tmp_path, monkeypatch, row_model=_BGE, extra_t3=(_CODE_TOKEN,),
        )
        assert [rig.run(), rig.run()] == [_CODE_TOKEN] * 2
        assert rig.total_renames == 0

    def test_a_failed_catalog_row_read_keeps_the_legacy_name(
        self, tmp_path, monkeypatch,
    ) -> None:
        """The row comes from the catalog the indexer holds; when that read
        fails the legacy collection is kept, never renamed blind."""
        rig = _MigrationRig(tmp_path, monkeypatch, row_model=None)
        rig.cat = _FailingRowCat()
        assert rig.run() == rig.legacy
        assert rig.total_renames == 0

    def test_the_catalog_row_decides_when_the_catalog_has_one(
        self, tmp_path, monkeypatch,
    ) -> None:
        rig = _MigrationRig(tmp_path, monkeypatch, row_model=None)
        rig.cat = _RowCat(_BGE)
        assert [rig.run(), rig.run()] == [rig.legacy] * 2
        assert rig.total_renames == 0


class TestIndexerMigrationWithAgreeingRow:
    def test_agreeing_row_renames_once_and_every_later_run_is_the_same_target(
        self, tmp_path, monkeypatch,
    ) -> None:
        """Migrate, apply the engine rename, migrate again, and again: one
        rename, one target. This is the shape the critic's two-run repro
        exercised with a disagreeing row; with an agreeing row it stays
        stable by construction."""
        rig = _MigrationRig(tmp_path, monkeypatch, row_model=_CODE_WRITE)
        first = rig.run()
        assert first == _CODE_TOKEN
        assert rig.rename.call_args.args[:2] == (rig.legacy, _CODE_TOKEN)
        assert rig.warnings(0) == []

        rig.engine_renames_to(first)
        assert rig.run() == _CODE_TOKEN
        assert rig.run() == _CODE_TOKEN
        assert rig.total_renames == 1  # legacy is gone: steady state

    def test_no_row_mints_from_the_write_model_as_before(
        self, tmp_path, monkeypatch,
    ) -> None:
        rig = _MigrationRig(tmp_path, monkeypatch, row_model=None)
        assert rig.run() == _CODE_TOKEN
        assert rig.rename.call_args.args[:2] == (rig.legacy, _CODE_TOKEN)

    def test_steady_state_when_the_legacy_equals_the_minted_name_is_untouched(
        self, tmp_path, monkeypatch,
    ) -> None:
        """No legacy collection in T3, only the conformant one (with its row):
        nothing renames and the result is the minted name, run after run."""
        rig = _MigrationRig(tmp_path, monkeypatch, row_model=None)
        rig.t3.names = {_CODE_TOKEN}
        rig.rows[_CODE_TOKEN] = _row(_CODE_WRITE, "code")
        assert [rig.run(), rig.run()] == [_CODE_TOKEN] * 2
        assert rig.total_renames == 0
        assert rig.warnings(0) == []
