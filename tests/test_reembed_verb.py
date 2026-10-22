# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-210 Phase 6 Step 3 (nexus-cnxjk): ``nx collection re-embed`` as the cross-model job's client.

A fake engine (``MagicMock(spec=HttpVectorClient)``, so a method the client lacks fails here too) and a fake
catalog reader stand in for the engine; nothing here reaches storage. Model tokens are inert: only a token's
prefix (``voyage`` or ``bge``) carries meaning to the code under test, and the RDR-109 mode census flags real
Voyage model names in a test body that does not declare cloud mode.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.db.http_vector_client import HttpVectorClient, VectorServiceError
from tests._module_seam import patch_time

BGE = "bge-base-en-v15-768"
VA = "voyage-model-a"
JOB = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def local_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NX_LOCAL", "1")


def _row(name, *, ct="docs", model=BGE, state="live", superseded_by="", owner="1.1"):
    return {
        "name": name, "content_type": ct, "owner_id": owner, "embedding_model": model,
        "lifecycle_state": state, "superseded_by": superseded_by,
    }


def _plan(target="docs__1-1__voyage-model-a__v1", **over):
    plan = {
        "source_collection": "docs__1-1__bge-base-en-v15-768__v1", "target_collection": target,
        "target_action": "registered", "documents": 4, "tombstoned_documents": 1, "chunks": 40,
        "ownerless_chunks": 2, "token_estimate": 9_000, "quarantine_rows": 6,
        "foreign_topic_assignments": 7, "topic_links": 8, "unmatched_satellite_rows": 9,
    }
    plan.update(over)
    return plan


def _job(job_id=JOB, *, state="planned", source="docs__a", target="docs__b", **over):
    job = {
        "job_id": job_id, "source_collection": source, "target_collection": target, "state": state,
        "disposition": None, "failure_reason": None, "last_error": None, "documents_moved": 0,
        "documents_skipped": 0, "chunks_embedded": 0, "chunks_reused": 0, "tokens_billed": 0,
        "token_cap": None,
    }
    job.update(over)
    return job


class FakeCatalog:
    def __init__(self, rows, profile, *, doc_counts=None, all_doc_counts=None):
        self.rows = {r["name"]: r for r in rows}
        self._profile = profile
        self._live = doc_counts or {}
        self._all = all_doc_counts or {}

    def get_collection(self, name):
        return self.rows.get(name)

    def list_collections(self, *a, **k):
        return list(self.rows.values())

    def embedding_profile(self):
        return [{"content_type": ct, "embedding_model": m, "dimension": 3} for ct, m in self._profile.items()]

    def collection_doc_counts(self, *, include_deleted=False):
        return dict(self._all if include_deleted else self._live)


def _engine(plans=None, *, served=None, jobs=None):
    """A fake engine. ``plans`` maps a source name to its plan body (or an exception to raise)."""
    db = MagicMock(spec=HttpVectorClient)
    db._resolve_collection_row.return_value = None
    db.served_embedding_models.return_value = served
    plans = plans or {}

    def plan(source, *, target_collection=None):
        out = plans.get(source, _plan())
        if isinstance(out, Exception):
            raise out
        return out

    db.reembed_plan.side_effect = plan
    db.reembed_start.side_effect = lambda source, **kw: _job(source=source, state="planned", target="docs__t")
    db.reembed_reenter.side_effect = lambda source: _job(source=source, state="cutover", target="docs__t")
    db.reembed_status.return_value = jobs if jobs is not None else []
    return db


def _invoke(runner, db, catalog, args):
    with patch("nexus.commands.collection._t3", return_value=db), \
            patch("nexus.catalog.factory.make_catalog_reader", return_value=catalog):
        return runner.invoke(main, ["collection", "re-embed", *args])


SRC = "docs__1-1__bge-base-en-v15-768__v1"


# ── the plan and its counts ──────────────────────────────────────────────────


class TestPlanOutput:
    def test_cross_model_dry_run_states_every_count_the_cutover_will_destroy(self, runner):
        db = _engine({SRC: _plan()})
        cat = FakeCatalog([_row(SRC)], {"docs": "voyage-3"})
        result = _invoke(runner, db, cat, [SRC, "--to", "voyage-3"])
        assert result.exit_code == 0, result.output
        out = result.output
        assert "4 live to move" in out and "1 tombstoned" in out
        assert "40 to embed" in out and "2 ownerless" in out
        assert "about 9,000 estimated, cap 10,000,000" in out
        assert "6 quarantine row(s)" in out
        assert "7 topic assignment(s) of other collections" in out and "8 topic link(s)" in out
        assert "satellite rows naming it that match no document the job moves: 9" in out
        assert "dry-run" in out
        db.reembed_start.assert_not_called()

    def test_the_plan_is_asked_with_the_rendered_target_name(self, runner):
        db = _engine({SRC: _plan()})
        cat = FakeCatalog([_row(SRC)], {"docs": "voyage-3"})
        _invoke(runner, db, cat, [SRC, "--to", "voyage-3"])
        db.reembed_plan.assert_called_once_with(SRC, target_collection="docs__1-1__voyage-3__v1")

    def test_a_bge_target_states_no_cap_and_sends_none(self, runner):
        src = "docs__1-1__voyage-model-a__v1"
        db = _engine({src: _plan(token_estimate=0)})
        cat = FakeCatalog([_row(src, model=VA)], {"docs": BGE})
        result = _invoke(runner, db, cat, [src, "--to", BGE, "--no-dry-run", "--yes", "--token-cap", "5"])
        assert result.exit_code == 0, result.output
        assert "no cap" in result.output and "--token-cap is not sent" in result.output
        assert db.reembed_start.call_args.kwargs["token_cap"] is None


# ── the start body ───────────────────────────────────────────────────────────


class TestStartBody:
    def test_flags_and_cap_and_target_ride_the_start_call(self, runner):
        db = _engine({SRC: _plan(target="docs__1-1__voyage-3__v1")})
        cat = FakeCatalog([_row(SRC)], {"docs": "voyage-3"})
        result = _invoke(runner, db, cat, [
            SRC, "--to", "voyage-3", "--no-dry-run", "--yes", "--keep-source", "--force-delete",
            "--token-cap", "20000",
        ])
        assert result.exit_code == 0, result.output
        db.reembed_start.assert_called_once_with(
            SRC, target_collection="docs__1-1__voyage-3__v1", token_cap=20_000, keep_source=True,
            force_delete=True,
        )
        assert f"started {JOB}" in result.output

    def test_defaults_send_the_default_voyage_cap_and_both_flags_false(self, runner):
        db = _engine({SRC: _plan(target="docs__1-1__voyage-3__v1")})
        cat = FakeCatalog([_row(SRC)], {"docs": "voyage-3"})
        _invoke(runner, db, cat, [SRC, "--to", "voyage-3", "--no-dry-run", "--yes"])
        kw = db.reembed_start.call_args.kwargs
        assert (kw["token_cap"], kw["keep_source"], kw["force_delete"]) == (10_000_000, False, False)

    def test_without_yes_it_asks_and_a_no_starts_nothing(self, runner):
        db = _engine({SRC: _plan()})
        cat = FakeCatalog([_row(SRC)], {"docs": "voyage-3"})
        with patch("nexus.commands.collection._t3", return_value=db), \
                patch("nexus.catalog.factory.make_catalog_reader", return_value=cat):
            result = runner.invoke(main, ["collection", "re-embed", SRC, "--to", "voyage-3", "--no-dry-run"], input="n\n")
        assert result.exit_code != 0
        db.reembed_start.assert_not_called()

    def test_a_resent_start_reports_the_job_that_holds_the_source(self, runner):
        db = _engine({SRC: _plan()})
        db.reembed_start.side_effect = VectorServiceError(
            "POST /v1/vectors/reembed/jobs -> HTTP 409: held", code=409,
            engine_body={"error": "re-embed job J (running): source docs__a is held by docs__a to docs__b",
                         "job_id": JOB, "state": "running"},
        )
        cat = FakeCatalog([_row(SRC)], {"docs": "voyage-3"})
        result = _invoke(runner, db, cat, [SRC, "--to", "voyage-3", "--no-dry-run", "--yes"])
        assert result.exit_code != 0
        assert JOB in result.output and "running" in result.output


# ── the cap and the other refusals, all before any write ─────────────────────


class TestRefusals:
    def test_a_plan_above_the_cap_is_refused_and_nothing_starts(self, runner):
        db = _engine({SRC: _plan(token_estimate=50_000)})
        cat = FakeCatalog([_row(SRC)], {"docs": "voyage-3"})
        result = _invoke(runner, db, cat, [SRC, "--to", "voyage-3", "--no-dry-run", "--yes", "--token-cap", "1000"])
        assert result.exit_code != 0
        assert "50,000" in result.output and "cap of 1,000" in result.output and "--token-cap" in result.output
        db.reembed_start.assert_not_called()

    def test_the_default_cap_refuses_a_plan_above_ten_million(self, runner):
        db = _engine({SRC: _plan(token_estimate=10_000_001)})
        cat = FakeCatalog([_row(SRC)], {"docs": "voyage-3"})
        result = _invoke(runner, db, cat, [SRC, "--to", "voyage-3", "--no-dry-run", "--yes"])
        assert result.exit_code != 0 and "cap of 10,000,000" in result.output
        db.reembed_start.assert_not_called()

    def test_raising_the_cap_lets_the_same_plan_start(self, runner):
        db = _engine({SRC: _plan(token_estimate=50_000)})
        cat = FakeCatalog([_row(SRC)], {"docs": "voyage-3"})
        result = _invoke(runner, db, cat, [SRC, "--to", "voyage-3", "--no-dry-run", "--yes", "--token-cap", "60000"])
        assert result.exit_code == 0, result.output
        db.reembed_start.assert_called_once()

    def test_the_dry_run_applies_the_same_cap_refusal(self, runner):
        db = _engine({SRC: _plan(token_estimate=50_000)})
        cat = FakeCatalog([_row(SRC)], {"docs": "voyage-3"})
        result = _invoke(runner, db, cat, [SRC, "--to", "voyage-3", "--token-cap", "1000"])
        assert result.exit_code != 0 and "cap of 1,000" in result.output

    def test_one_source_over_the_cap_stops_every_start_of_an_all_run(self, runner):
        a, b = "docs__1-1__bge-base-en-v15-768__v1", "docs__2-2__bge-base-en-v15-768__v1"
        db = _engine({a: _plan(token_estimate=10), b: _plan(token_estimate=99_999_999)})
        cat = FakeCatalog([_row(a, owner="1.1"), _row(b, owner="2.2")], {"docs": VA})
        result = _invoke(runner, db, cat, ["--all", "--no-dry-run", "--yes"])
        assert result.exit_code != 0
        assert "99,999,999" in result.output
        db.reembed_start.assert_not_called()

    def test_an_engine_that_cannot_serve_the_target_model_is_refused_naming_the_missing_piece(self, runner):
        db = _engine({SRC: _plan()}, served=[BGE])
        cat = FakeCatalog([_row(SRC)], {"docs": VA})
        result = _invoke(runner, db, cat, ["--all", "--no-dry-run", "--yes"])
        assert result.exit_code != 0
        assert f"does not serve {VA}" in result.output and "voyage_api_key" in result.output
        assert "nx daemon service stop && nx daemon service start" in result.output
        db.reembed_plan.assert_not_called()
        db.reembed_start.assert_not_called()

    def test_an_engine_that_reports_no_models_is_not_read_as_missing(self, runner):
        db = _engine({SRC: _plan(target="docs__1-1__voyage-model-a__v1")}, served=None)
        result = _invoke(runner, db, FakeCatalog([_row(SRC)], {"docs": VA}), ["--all"])
        assert result.exit_code == 0, result.output
        db.reembed_plan.assert_called_once()

    def test_a_to_that_is_not_the_profile_model_is_refused(self, runner):
        db = _engine()
        result = _invoke(runner, db, FakeCatalog([_row(SRC)], {"docs": VA}), [SRC, "--to", "voyage-3"])
        assert result.exit_code != 0 and VA in result.output
        db.reembed_plan.assert_not_called()

    def test_the_engines_plan_refusal_is_the_sources_reason(self, runner):
        err = VectorServiceError(
            "HTTP 409", code=409,
            engine_body={"error": "re-embed job J (failed): source docs__a is held by docs__a to docs__b",
                         "job_id": JOB, "state": "failed"},
        )
        db = _engine({SRC: err})
        result = _invoke(runner, db, FakeCatalog([_row(SRC)], {"docs": VA}), ["--all"])
        assert result.exit_code != 0
        assert JOB in result.output and "failed" in result.output

    def test_a_superseded_or_quarantined_named_source_is_refused(self, runner):
        for kw in ({"superseded_by": "docs__x"}, {"state": "quarantine"}):
            db = _engine()
            cat = FakeCatalog([_row(SRC, **kw)], {"docs": "voyage-3"})
            result = _invoke(runner, db, cat, [SRC, "--to", "voyage-3"])
            assert result.exit_code != 0 and "not a live, current collection" in result.output
            db.reembed_plan.assert_not_called()

    def test_503_from_an_engine_without_the_local_variable_says_so(self, runner):
        err = VectorServiceError(
            "HTTP 503", code=503, engine_body={"error": "the re-embed job runs on a local engine only"},
        )
        db = _engine({SRC: err})
        result = _invoke(runner, db, FakeCatalog([_row(SRC)], {"docs": VA}), ["--all"])
        assert result.exit_code != 0 and "local engine only" in result.output

    def test_404_from_an_engine_below_the_routes_says_to_upgrade(self, runner):
        db = _engine({SRC: VectorServiceError("HTTP 404", code=404, engine_body={"error": "not found"})})
        result = _invoke(runner, db, FakeCatalog([_row(SRC)], {"docs": VA}), ["--all"])
        assert result.exit_code != 0 and "upgrade" in result.output


# ── mode ─────────────────────────────────────────────────────────────────────


class TestLocalModeOnly:
    def test_the_verb_refuses_every_job_form_when_not_local(self, runner, cloud_mode):
        for args in (["--all"], ["--status"], ["--pause", JOB], ["--resume", JOB], ["--abort", JOB]):
            db = _engine()
            result = _invoke(runner, db, FakeCatalog([], {}), args)
            assert result.exit_code != 0, args
            assert "local engine only" in result.output, args
            db.reembed_plan.assert_not_called()
            db.reembed_status.assert_not_called()
            db.reembed_start.assert_not_called()

    def test_a_cross_model_to_outside_local_mode_keeps_the_old_refusal(self, runner, cloud_mode):
        db = _engine()
        db._resolve_collection_row.return_value = {"embedding_model": BGE}
        result = _invoke(runner, db, FakeCatalog([_row(SRC)], {"docs": VA}), [SRC, "--to", "voyage-3"])
        assert result.exit_code != 0
        assert "catalog row" in result.output and "cannot take effect" in result.output
        db.reembed_plan.assert_not_called()


# ── --all candidacy ──────────────────────────────────────────────────────────


class TestAllCandidacy:
    def test_lists_only_live_stale_collections_and_names_a_held_one(self, runner):
        stale = "docs__1-1__bge-base-en-v15-768__v1"
        held = "docs__3-3__bge-base-en-v15-768__v1"
        rows = [
            _row(stale, owner="1.1"),
            _row("docs__2-2__voyage-model-a__v1", model=VA, owner="2.2"),          # on profile
            _row(held, owner="3.3"),                                                # a job holds it
            _row("docs__4-4__bge-base-en-v15-768__v1", owner="4.4", state="quarantine"),
            _row("docs__5-5__bge-base-en-v15-768__v1", owner="5.5", superseded_by="docs__x"),
        ]
        jobs = [_job(JOB, source=held, target="docs__held-t", state="running")]
        db = _engine({stale: _plan()}, jobs=jobs)
        result = _invoke(runner, db, FakeCatalog(rows, {"docs": VA}), ["--all"])
        assert result.exit_code == 0, result.output
        planned = [c.args[0] for c in db.reembed_plan.call_args_list]
        assert planned == [stale]
        assert f"held by job {JOB}" in result.output
        assert "1 job(s) would start" in result.output
        db.reembed_start.assert_not_called()

    def test_starts_one_job_per_stale_collection(self, runner):
        a, b = "docs__1-1__bge-base-en-v15-768__v1", "docs__2-2__bge-base-en-v15-768__v1"
        db = _engine({a: _plan(), b: _plan()})
        cat = FakeCatalog([_row(a, owner="1.1"), _row(b, owner="2.2")], {"docs": VA})
        result = _invoke(runner, db, cat, ["--all", "--no-dry-run", "--yes", "--keep-source"])
        assert result.exit_code == 0, result.output
        assert [c.args[0] for c in db.reembed_start.call_args_list] == [a, b]
        assert all(c.kwargs["keep_source"] is True for c in db.reembed_start.call_args_list)

    def test_nothing_stale_says_so(self, runner):
        db = _engine()
        result = _invoke(runner, db, FakeCatalog([_row("docs__x", model=VA)], {"docs": VA}), ["--all"])
        assert result.exit_code == 0 and "No collection is off the profile" in result.output

    def test_kept_tombstoned_source_with_no_document_row_is_listed_as_a_reentry(self, runner):
        src, tgt = "docs__1-1__bge-base-en-v15-768__v1", "docs__1-1__voyage-model-a__v1"
        jobs = [_job(JOB, source=src, target=tgt, state="done", disposition="kept_tombstoned")]
        cat = FakeCatalog([_row(src, superseded_by=tgt)], {"docs": VA}, doc_counts={}, all_doc_counts={})
        db = _engine(jobs=jobs)
        result = _invoke(runner, db, cat, ["--all"])
        assert result.exit_code == 0, result.output
        assert f"re-entry of job {JOB} (cutover)" in result.output
        db.reembed_plan.assert_not_called()

    def test_kept_tombstoned_source_with_a_restored_live_document_is_a_running_reentry(self, runner):
        src, tgt = "docs__1-1__bge-base-en-v15-768__v1", "docs__1-1__voyage-model-a__v1"
        jobs = [_job(JOB, source=src, target=tgt, state="done", disposition="kept_tombstoned")]
        cat = FakeCatalog([_row(src, superseded_by=tgt)], {"docs": VA}, doc_counts={src: 1}, all_doc_counts={src: 2})
        result = _invoke(runner, _engine(jobs=jobs), cat, ["--all"])
        assert f"re-entry of job {JOB} (running)" in result.output

    @pytest.mark.parametrize("disposition", ["kept_by_flag", "kept_residue"])
    def test_kept_by_flag_and_kept_residue_sources_are_never_listed(self, runner, disposition):
        src, tgt = "docs__1-1__bge-base-en-v15-768__v1", "docs__1-1__voyage-model-a__v1"
        jobs = [_job(JOB, source=src, target=tgt, state="done", disposition=disposition)]
        cat = FakeCatalog([_row(src, superseded_by=tgt)], {"docs": VA}, doc_counts={}, all_doc_counts={})
        db = _engine(jobs=jobs)
        result = _invoke(runner, db, cat, ["--all"])
        assert result.exit_code == 0, result.output
        assert "re-entry" not in result.output and src not in result.output
        db.reembed_plan.assert_not_called()

    def test_all_takes_no_name_or_to(self, runner):
        result = _invoke(runner, _engine(), FakeCatalog([], {}), ["--all", "--to", "voyage-3"])
        assert result.exit_code != 0 and "takes no NAME or --to" in result.output


class TestAllSkip:
    """nexus-kyzmw: ``--all --skip NAME`` leaves a source out of the run, so one permanent refusal no longer makes
    --all unusable. The default stays all-or-nothing: a refused plan still starts nothing."""

    A = "docs__1-1__bge-base-en-v15-768__v1"
    B = "docs__2-2__bge-base-en-v15-768__v1"

    def _cat(self):
        return FakeCatalog([_row(self.A, owner="1.1"), _row(self.B, owner="2.2")], {"docs": VA})

    def test_a_skipped_source_over_the_cap_no_longer_stops_the_rest(self, runner):
        db = _engine({self.A: _plan(token_estimate=10), self.B: _plan(token_estimate=99_999_999)})
        result = _invoke(runner, db, self._cat(), ["--all", "--skip", self.B, "--no-dry-run", "--yes"])
        assert result.exit_code == 0, result.output
        assert [c.args[0] for c in db.reembed_plan.call_args_list] == [self.A], "a skipped source is not planned"
        assert [c.args[0] for c in db.reembed_start.call_args_list] == [self.A]
        assert f"{self.B}: skipped (--skip)" in result.output

    def test_without_skip_the_same_refusal_still_starts_nothing(self, runner):
        db = _engine({self.A: _plan(token_estimate=10), self.B: _plan(token_estimate=99_999_999)})
        result = _invoke(runner, db, self._cat(), ["--all", "--no-dry-run", "--yes"])
        assert result.exit_code != 0
        db.reembed_start.assert_not_called()

    def test_skip_is_repeatable_and_skipping_everything_says_so(self, runner):
        db = _engine()
        result = _invoke(runner, db, self._cat(), ["--all", "--skip", self.A, "--skip", self.B])
        assert result.exit_code == 0, result.output
        db.reembed_plan.assert_not_called()
        assert f"{self.A}: skipped (--skip)" in result.output and f"{self.B}: skipped (--skip)" in result.output
        assert "No collection is off the profile" not in result.output

    def test_a_name_all_would_not_plan_is_an_error_and_nothing_is_planned(self, runner):
        db = _engine()
        result = _invoke(runner, db, self._cat(), ["--all", "--skip", "docs__nope"])
        assert result.exit_code != 0
        assert "docs__nope" in result.output and "not one of --all's candidates" in result.output
        assert self.A in result.output and self.B in result.output
        db.reembed_plan.assert_not_called()

    def test_a_held_source_can_be_skipped(self, runner):
        jobs = [_job(JOB, source=self.B, target="docs__held-t", state="running")]
        db = _engine({self.A: _plan()}, jobs=jobs)
        result = _invoke(runner, db, self._cat(), ["--all", "--skip", self.B])
        assert result.exit_code == 0, result.output
        assert "held by job" not in result.output and f"{self.B}: skipped (--skip)" in result.output

    def test_a_reentry_source_can_be_skipped(self, runner):
        src, tgt = "docs__9-9__bge-base-en-v15-768__v1", "docs__9-9__voyage-model-a__v1"
        jobs = [_job(JOB, source=src, target=tgt, state="done", disposition="kept_tombstoned")]
        cat = FakeCatalog([_row(src, superseded_by=tgt)], {"docs": VA}, doc_counts={}, all_doc_counts={})
        result = _invoke(runner, _engine(jobs=jobs), cat, ["--all", "--skip", src])
        assert result.exit_code == 0, result.output
        assert "re-entry of job" not in result.output and f"{src}: skipped (--skip)" in result.output

    def test_skipping_one_source_leaves_a_refusal_on_another_stopping_the_run(self, runner):
        c = "docs__3-3__bge-base-en-v15-768__v1"
        db = _engine({self.A: _plan(token_estimate=10), self.B: _plan(token_estimate=10),
                      c: _plan(token_estimate=99_999_999)})
        cat = FakeCatalog([_row(self.A, owner="1.1"), _row(self.B, owner="2.2"), _row(c, owner="3.3")], {"docs": VA})
        result = _invoke(runner, db, cat, ["--all", "--skip", self.B, "--no-dry-run", "--yes"])
        assert result.exit_code != 0 and "99,999,999" in result.output
        db.reembed_start.assert_not_called()

    @pytest.mark.parametrize("args", [["--status"], ["--pause", JOB]])
    def test_skip_with_a_job_control_is_a_usage_error(self, runner, args):
        db = _engine()
        result = _invoke(runner, db, self._cat(), [*args, "--skip", self.A])
        assert result.exit_code != 0 and "--skip goes with --all" in result.output
        db.reembed_status.assert_not_called()

    def test_skip_goes_with_all_only(self, runner):
        result = _invoke(runner, _engine(), self._cat(), [self.A, "--to", BGE, "--skip", self.B])
        assert result.exit_code != 0 and "--skip goes with --all" in result.output


# ── the controls ─────────────────────────────────────────────────────────────


class TestControls:
    def test_status_with_no_id_lists_every_job(self, runner):
        jobs = [
            _job("a-1", state="running", documents_moved=3, token_cap=100, tokens_billed=40),
            _job("b-2", state="failed", failure_reason="token_cap", last_error="cap reached"),
        ]
        db = _engine(jobs=jobs)
        result = _invoke(runner, db, FakeCatalog([], {}), ["--status"])
        assert result.exit_code == 0, result.output
        assert "a-1  running" in result.output and "moved 3" in result.output and "tokens 40 of 100" in result.output
        assert "b-2  failed (token_cap)" in result.output and "last error: cap reached" in result.output

    def test_status_with_an_id_shows_that_job_and_an_unknown_id_is_an_error(self, runner):
        db = _engine(jobs=[_job("a-1"), _job("b-2")])
        ok = _invoke(runner, db, FakeCatalog([], {}), ["--status", "b-2"])
        assert ok.exit_code == 0 and "b-2" in ok.output and "a-1" not in ok.output
        missing = _invoke(runner, db, FakeCatalog([], {}), ["--status", "zz"])
        assert missing.exit_code != 0 and "no re-embed job zz" in missing.output

    def test_status_with_no_jobs_says_so(self, runner):
        result = _invoke(runner, _engine(jobs=[]), FakeCatalog([], {}), ["--status"])
        assert result.exit_code == 0 and "No re-embed jobs" in result.output

    @pytest.mark.parametrize(("flag", "method", "state"), [
        ("--pause", "reembed_pause", "paused"), ("--resume", "reembed_resume", "running"),
        ("--abort", "reembed_abort", "aborted"),
    ])
    def test_each_control_calls_its_route_with_the_job_id(self, runner, flag, method, state):
        db = _engine()
        getattr(db, method).return_value = _job(JOB, state=state)
        result = _invoke(runner, db, FakeCatalog([], {}), [flag, JOB])
        assert result.exit_code == 0, result.output
        getattr(db, method).assert_called_once()
        assert getattr(db, method).call_args.args == (JOB,)
        assert f"{JOB}  {state}" in result.output

    def test_resume_carries_the_raised_cap(self, runner):
        db = _engine()
        db.reembed_resume.return_value = _job(JOB, state="running")
        _invoke(runner, db, FakeCatalog([], {}), ["--resume", JOB, "--token-cap", "99"])
        db.reembed_resume.assert_called_once_with(JOB, token_cap=99)

    def test_a_repeat_says_nothing_changed(self, runner):
        db = _engine()
        db.reembed_pause.return_value = {**_job(JOB, state="paused"), "unchanged": True}
        result = _invoke(runner, db, FakeCatalog([], {}), ["--pause", JOB])
        assert result.exit_code == 0 and "nothing changed" in result.output

    def test_a_refused_transition_surfaces_the_engines_words(self, runner):
        db = _engine()
        db.reembed_abort.side_effect = VectorServiceError(
            "HTTP 409", code=409, engine_body={
                "error": "abort is refused: the delete step has started", "job_id": JOB, "state": "cutover"},
        )
        result = _invoke(runner, db, FakeCatalog([], {}), ["--abort", JOB])
        assert result.exit_code != 0
        assert "delete step has started" in result.output and "cutover" in result.output

    def test_controls_stand_alone(self, runner):
        for args in (["--pause", JOB, "--abort", JOB], ["--status", "--all"], ["--pause", JOB, SRC],
                     ["--pause", JOB, "--keep-source"]):
            result = _invoke(runner, _engine(), FakeCatalog([], {}), args)
            assert result.exit_code != 0, args
        assert "--token-cap" in _invoke(
            runner, _engine(), FakeCatalog([], {}), ["--pause", JOB, "--token-cap", "5"]).output


# ── the in-place form is unchanged ───────────────────────────────────────────


class TestInPlaceUnchanged:
    def test_job_flags_with_a_same_model_to_are_a_usage_error(self, runner):
        db = _engine()
        db._resolve_collection_row.return_value = {"embedding_model": "voyage-3"}
        result = _invoke(runner, db, FakeCatalog([], {}), ["docs__x", "--to", "voyage-3", "--keep-source"])
        assert result.exit_code != 0 and "belong to the re-embed job" in result.output

    def test_name_without_to_is_a_usage_error(self, runner):
        result = _invoke(runner, _engine(), FakeCatalog([], {}), ["docs__x"])
        assert result.exit_code != 0 and "--to MODEL" in result.output

    def test_the_choices_gain_the_bge_model(self, runner):
        result = runner.invoke(main, ["collection", "re-embed", "--help"])
        assert BGE in result.output


# ── review fold-in (nexus-cnxjk) ─────────────────────────────────────────────


class TestEngine404:
    """A 404 means "upgrade the engine" only for a body the handler does not itself send."""

    @pytest.mark.parametrize("flag", ["--pause", "--resume", "--abort"])
    def test_an_unknown_job_is_shown_verbatim_not_as_an_upgrade_hint(self, runner, flag):
        db = _engine()
        err = VectorServiceError("HTTP 404", code=404, engine_body={"error": f"no re-embed job {JOB}"})
        for method in ("reembed_pause", "reembed_resume", "reembed_abort"):
            getattr(db, method).side_effect = err
        result = _invoke(runner, db, FakeCatalog([], {}), [flag, JOB])
        assert result.exit_code != 0
        assert f"no re-embed job {JOB}" in result.output and "upgrade" not in result.output

    def test_an_unregistered_source_is_shown_verbatim(self, runner):
        err = VectorServiceError(
            "HTTP 404", code=404, engine_body={"error": f"source collection {SRC} is not registered"},
        )
        db = _engine({SRC: err})
        result = _invoke(runner, db, FakeCatalog([_row(SRC)], {"docs": VA}), ["--all"])
        assert result.exit_code != 0
        assert f"source collection {SRC} is not registered" in result.output and "upgrade" not in result.output

    @pytest.mark.parametrize("body", [{"error": "not found: /plan"}, {"error": "not found"}, None])
    def test_a_route_the_engine_lacks_still_says_upgrade(self, runner, body):
        db = _engine({SRC: VectorServiceError("HTTP 404", code=404, engine_body=body)})
        result = _invoke(runner, db, FakeCatalog([_row(SRC)], {"docs": VA}), ["--all"])
        assert result.exit_code != 0 and "upgrade" in result.output


class TestOwnerlessTargetName:
    def test_a_row_with_no_owner_is_refused_before_the_plan_and_names_no_none(self, runner):
        db = _engine()
        row = _row(SRC)
        row["owner_id"] = None
        result = _invoke(runner, db, FakeCatalog([row], {"docs": VA}), ["--all", "--no-dry-run", "--yes"])
        assert result.exit_code != 0
        assert "no owner_id" in result.output and "None" not in result.output
        db.reembed_plan.assert_not_called()
        db.reembed_start.assert_not_called()

    def test_a_row_whose_owner_is_the_tenant_id_is_refused(self, runner):
        db = _engine()
        db._tenant = "tenant-7"
        row = _row(SRC, owner="tenant-7")
        result = _invoke(runner, db, FakeCatalog([row], {"docs": VA}), ["--all"])
        assert result.exit_code != 0 and "placeholder 'tenant-7'" in result.output
        db.reembed_plan.assert_not_called()

    def test_a_row_whose_own_tenant_id_column_is_its_owner_is_refused(self, runner):
        db = _engine()
        row = _row(SRC, owner="tenant-9")
        row["tenant_id"] = "tenant-9"
        result = _invoke(runner, db, FakeCatalog([row], {"docs": VA}), ["--all"])
        assert result.exit_code != 0 and "placeholder" in result.output

    def test_the_default_owner_is_refused(self, runner):
        db = _engine()
        result = _invoke(runner, db, FakeCatalog([_row(SRC, owner="default")], {"docs": VA}), ["--all"])
        assert result.exit_code != 0 and "placeholder 'default'" in result.output
        db.reembed_plan.assert_not_called()


class TestPlanWording:
    def test_the_plan_states_the_live_documents_homed_here_whose_chunks_are_elsewhere(self, runner):
        db = _engine({SRC: _plan(live_homed_elsewhere=3)})
        result = _invoke(runner, db, FakeCatalog([_row(SRC)], {"docs": VA}), ["--all"])
        assert result.exit_code == 0, result.output
        assert "live documents homed here whose chunks sit in another collection: 3" in result.output
        assert "does not predict" not in result.output and "does not count" not in result.output

    def test_an_engine_that_sends_no_count_is_not_shown_as_zero(self, runner):
        plan = _plan()
        assert "live_homed_elsewhere" not in plan
        db = _engine({SRC: plan})
        result = _invoke(runner, db, FakeCatalog([_row(SRC)], {"docs": VA}), ["--all"])
        assert result.exit_code == 0, result.output
        assert "chunks sit in another collection: 0" not in result.output
        assert "does not count live documents homed here" in result.output


class TestConfirmPrompt:
    def _decline(self, runner, db, cat, args):
        with patch("nexus.commands.collection._t3", return_value=db), \
                patch("nexus.catalog.factory.make_catalog_reader", return_value=cat):
            return runner.invoke(main, ["collection", "re-embed", *args], input="n\n")

    def test_the_all_prompt_states_the_total_estimate_and_the_per_job_cap(self, runner):
        a, b = "docs__1-1__bge-base-en-v15-768__v1", "docs__2-2__bge-base-en-v15-768__v1"
        db = _engine({a: _plan(token_estimate=9_000), b: _plan(token_estimate=4_000)})
        cat = FakeCatalog([_row(a, owner="1.1"), _row(b, owner="2.2")], {"docs": VA})
        result = self._decline(runner, db, cat, ["--all", "--no-dry-run"])
        assert result.exit_code != 0
        assert "Start 2 re-embed job(s)?" in result.output
        assert "About 13,000 tokens will be billed in all" in result.output
        assert "capped at 10,000,000 tokens on its own" in result.output
        db.reembed_start.assert_not_called()

    def test_a_raised_cap_is_the_cap_the_prompt_names(self, runner):
        db = _engine({SRC: _plan(token_estimate=500)})
        cat = FakeCatalog([_row(SRC)], {"docs": VA})
        result = self._decline(runner, db, cat, ["--all", "--no-dry-run", "--token-cap", "777"])
        assert "capped at 777 tokens on its own" in result.output

    def test_an_on_device_target_bills_nothing_and_the_prompt_says_so(self, runner):
        src = "docs__1-1__voyage-model-a__v1"
        db = _engine({src: _plan(chunks=40, token_estimate=0)})
        cat = FakeCatalog([_row(src, model=VA)], {"docs": BGE})
        result = self._decline(runner, db, cat, ["--all", "--no-dry-run"])
        assert "40 chunk(s) are embedded on this machine and bill nothing" in result.output
        assert "will be billed" not in result.output


# ── cutover and source removal (nexus-09iho) ─────────────────────────────────


KEPT_SRC, KEPT_TGT = "docs__1-1__bge-base-en-v15-768__v1", "docs__1-1__voyage-model-a__v1"


def _kept_tombstoned_world(*, live=0, total=0):
    jobs = [_job(JOB, source=KEPT_SRC, target=KEPT_TGT, state="done", disposition="kept_tombstoned")]
    cat = FakeCatalog([_row(KEPT_SRC, superseded_by=KEPT_TGT)], {"docs": VA},
                      doc_counts={KEPT_SRC: live} if live else {}, all_doc_counts={KEPT_SRC: total} if total else {})
    return jobs, cat


class TestReentryStarts:
    """``--all`` lists a kept_tombstoned source's re-entry and, with --no-dry-run, starts it through the engine's
    start route with the source alone: the job keeps its own target, cap and flags."""

    @pytest.mark.parametrize(("live", "total", "kind"), [(0, 0, "cutover"), (1, 2, "running")])
    def test_the_reentry_is_started_with_the_source_alone(self, runner, live, total, kind):
        jobs, cat = _kept_tombstoned_world(live=live, total=total)
        db = _engine(jobs=jobs)
        result = _invoke(runner, db, cat, ["--all", "--no-dry-run", "--yes"])
        assert result.exit_code == 0, result.output
        db.reembed_reenter.assert_called_once_with(KEPT_SRC)
        db.reembed_start.assert_not_called()
        db.reembed_plan.assert_not_called()
        assert f"re-entry of job {JOB} ({kind})" in result.output
        assert "re-entered" in result.output

    def test_a_dry_run_names_the_reentry_and_writes_nothing(self, runner):
        jobs, cat = _kept_tombstoned_world()
        db = _engine(jobs=jobs)
        result = _invoke(runner, db, cat, ["--all"])
        assert result.exit_code == 0, result.output
        assert "1 would be re-entered" in result.output and "dry-run" in result.output
        db.reembed_reenter.assert_not_called()

    def test_a_refused_reentry_is_reported_and_exits_nonzero(self, runner):
        jobs, cat = _kept_tombstoned_world()
        db = _engine(jobs=jobs)
        db.reembed_reenter.side_effect = VectorServiceError(
            "HTTP 409", code=409, engine_body={"error": "source holds 1 tombstoned document(s) and no live one",
                                               "job_id": JOB, "state": "done"})
        result = _invoke(runner, db, cat, ["--all", "--no-dry-run", "--yes"])
        assert result.exit_code != 0
        assert "not re-entered" in result.output and "1 tombstoned document(s)" in result.output

    def test_job_flags_do_not_reach_a_reentry_and_the_run_says_so(self, runner):
        jobs, cat = _kept_tombstoned_world()
        db = _engine(jobs=jobs)
        result = _invoke(runner, db, cat, ["--all", "--no-dry-run", "--yes", "--keep-source"])
        assert result.exit_code == 0, result.output
        assert "not to a re-entered job" in result.output
        db.reembed_reenter.assert_called_once_with(KEPT_SRC)

    def test_a_stale_source_and_a_reentry_start_together(self, runner):
        stale = "docs__2-2__bge-base-en-v15-768__v1"
        jobs, _ = _kept_tombstoned_world()
        cat = FakeCatalog([_row(stale, owner="2.2"), _row(KEPT_SRC, superseded_by=KEPT_TGT)], {"docs": VA})
        db = _engine({stale: _plan()}, jobs=jobs)
        result = _invoke(runner, db, cat, ["--all", "--no-dry-run", "--yes"])
        assert result.exit_code == 0, result.output
        assert [c.args[0] for c in db.reembed_start.call_args_list] == [stale]
        db.reembed_reenter.assert_called_once_with(KEPT_SRC)


class TestWait:
    """``--wait`` returns on done, done_with_skips, aborted, failed and paused, naming the state, and runs taxonomy
    discover for the target on the first two."""

    SRC_A = "docs__1-1__bge-base-en-v15-768__v1"

    def _world(self, final_state, *, polls=("running", "cutover")):
        """A fake engine whose status walks through ``polls`` and then ``final_state``."""
        db = _engine({self.SRC_A: _plan()})
        db.reembed_start.side_effect = lambda source, **kw: _job(JOB, source=source, state="planned", target="docs__t")
        walk = [[_job(JOB, source=self.SRC_A, state=s, target="docs__t")] for s in (*polls, final_state)]
        db.reembed_status.side_effect = [[], *walk]    # the first read is --all's candidacy: no job yet
        return db, FakeCatalog([_row(self.SRC_A)], {"docs": VA})

    def _run(self, runner, db, cat, args):
        with patch_time("nexus.commands.collection", "sleep") as sleep, patch("nexus.commands.collection._run_taxonomy_discover",
                                                 return_value=True) as discover:
            result = _invoke(runner, db, cat, args)
        return result, sleep, discover

    @pytest.mark.parametrize("state", ["done", "done_with_skips"])
    def test_a_finished_job_is_named_and_its_target_is_discovered(self, runner, state):
        db, cat = self._world(state)
        result, sleep, discover = self._run(runner, db, cat, ["--all", "--no-dry-run", "--yes", "--wait"])
        assert result.exit_code == 0, result.output
        assert f"-> {state}" in result.output
        discover.assert_called_once_with("docs__t")
        assert sleep.call_count == 2, "polled through running and cutover before it returned"

    @pytest.mark.parametrize("state", ["aborted", "failed", "paused"])
    def test_a_job_that_stopped_short_is_named_exits_one_and_runs_no_discover(self, runner, state):
        db, cat = self._world(state)
        result, _sleep, discover = self._run(runner, db, cat, ["--all", "--no-dry-run", "--yes", "--wait"])
        assert result.exit_code == 1, result.output
        assert f"-> {state}" in result.output
        discover.assert_not_called()

    def test_without_wait_the_start_names_the_discover_command_and_does_not_poll(self, runner):
        db, cat = self._world("done")
        result, sleep, discover = self._run(runner, db, cat, ["--all", "--no-dry-run", "--yes"])
        assert result.exit_code == 0, result.output
        assert "nx taxonomy discover --collection docs__t" in result.output
        sleep.assert_not_called()
        discover.assert_not_called()
        db.reembed_status.assert_called_once()   # the --all candidacy read, nothing more

    def test_a_done_job_in_status_names_the_discover_command(self, runner):
        jobs = [_job(JOB, state="done", target="docs__t")]
        result = _invoke(runner, _engine(jobs=jobs), FakeCatalog([], {}), ["--status"])
        assert "next: nx taxonomy discover --collection docs__t" in result.output

    def test_resume_wait_follows_the_resumed_job(self, runner):
        db = _engine()
        db.reembed_resume.return_value = _job(JOB, state="running", target="docs__t")
        db.reembed_status.side_effect = [[_job(JOB, state="running")], [_job(JOB, state="done", target="docs__t")]]
        with patch_time("nexus.commands.collection", "sleep"), patch("nexus.commands.collection._run_taxonomy_discover",
                                        return_value=True) as discover:
            result = _invoke(runner, db, FakeCatalog([], {}), ["--resume", JOB, "--wait"])
        assert result.exit_code == 0, result.output
        discover.assert_called_once_with("docs__t")

    def test_two_jobs_with_one_target_discover_it_once(self, runner):
        a, b = "docs__1-1__bge-base-en-v15-768__v1", "docs__2-2__bge-base-en-v15-768__v1"
        db = _engine({a: _plan(), b: _plan()})
        ids = {a: "j-a", b: "j-b"}
        db.reembed_start.side_effect = lambda source, **kw: _job(ids[source], source=source, target="docs__t")
        db.reembed_status.side_effect = [
            [],
            [_job("j-a", source=a, state="done", target="docs__t"), _job("j-b", source=b, state="running")],
            [_job("j-a", source=a, state="done", target="docs__t"),
             _job("j-b", source=b, state="done_with_skips", target="docs__t")],
        ]
        cat = FakeCatalog([_row(a, owner="1.1"), _row(b, owner="2.2")], {"docs": VA})
        with patch_time("nexus.commands.collection", "sleep"), patch("nexus.commands.collection._run_taxonomy_discover",
                                        return_value=True) as discover:
            result = _invoke(runner, db, cat, ["--all", "--no-dry-run", "--yes", "--wait"])
        assert result.exit_code == 0, result.output
        discover.assert_called_once_with("docs__t")

    def test_a_failed_discover_exits_one_though_the_job_itself_finished(self, runner):
        db, cat = self._world("done")
        with patch_time("nexus.commands.collection", "sleep"), patch("nexus.commands.collection._run_taxonomy_discover",
                                                                      return_value=False) as discover:
            result = _invoke(runner, db, cat, ["--all", "--no-dry-run", "--yes", "--wait"])
        assert result.exit_code == 1, result.output
        assert "-> done" in result.output
        discover.assert_called_once()

    def test_a_job_parked_in_a_blocked_cutover_returns_with_its_last_error_and_exit_one(self, runner):
        db, cat = self._world("cutover", polls=())
        blocked = _job(JOB, source=self.SRC_A, state="cutover", target="docs__t",
                       last_error="cutover is blocked: the source is no longer registered; abort the job")
        db.reembed_status.side_effect = [[], [blocked]]
        result, sleep, discover = self._run(runner, db, cat, ["--all", "--no-dry-run", "--yes", "--wait"])
        assert result.exit_code == 1, result.output
        assert "cutover is blocked: the source is no longer registered" in result.output
        assert "-> cutover (blocked" in result.output
        sleep.assert_not_called()
        discover.assert_not_called()

    def test_a_dry_run_has_nothing_to_wait_for_and_says_so(self, runner):
        db, cat = self._world("done")
        result, sleep, _ = self._run(runner, db, cat, ["--all", "--wait"])
        assert result.exit_code == 0, result.output
        assert "nothing to wait for in a dry run" in result.output
        sleep.assert_not_called()

    @pytest.mark.parametrize("args", [["--status", "--wait"], ["--pause", JOB, "--wait"], ["--abort", JOB, "--wait"]])
    def test_wait_goes_with_a_start_or_resume_only(self, runner, args):
        result = _invoke(runner, _engine(), FakeCatalog([], {}), args)
        assert result.exit_code != 0 and "--wait goes with a start" in result.output

    def test_wait_on_an_in_place_reembed_is_a_usage_error(self, runner):
        db = _engine()
        db._resolve_collection_row.return_value = {"embedding_model": "voyage-3"}
        result = _invoke(runner, db, FakeCatalog([], {}), ["docs__x", "--to", "voyage-3", "--wait"])
        assert result.exit_code != 0 and "has no job" in result.output


class TestStatusCounts:
    def test_status_asks_for_counts_and_prints_what_the_delete_removes(self, runner):
        counts = {"quarantine_rows": 6, "foreign_topic_assignments": 7, "topic_links": 8,
                  "unmatched_satellite_rows": 9}
        db = _engine(jobs=[_job(JOB, state="cutover", delete_counts=counts), _job("b-2", state="aborted",
                                                                                  delete_counts=None)])
        result = _invoke(runner, db, FakeCatalog([], {}), ["--status"])
        assert result.exit_code == 0, result.output
        db.reembed_status.assert_called_with(counts=True)
        assert ("the source's delete removes: 6 quarantine row(s); 7 topic assignment(s) of other collections; "
                "8 topic link(s); 9 satellite row(s) that match no moved document") in result.output
        assert result.output.count("the source's delete removes") == 1, "a job with none shows none"

    def test_an_engine_that_predates_the_counts_still_lists_its_jobs(self, runner):
        result = _invoke(runner, _engine(jobs=[_job(JOB, state="running")]), FakeCatalog([], {}), ["--status"])
        assert result.exit_code == 0 and "the source's delete removes" not in result.output


class TestRunTaxonomyDiscover:
    """``--wait``'s discover is ``nx taxonomy discover --collection TARGET`` run in-process, so it builds the target's
    topics the way ``nx index`` does; a failure is told, with the command to retry, and never raised."""

    def _wire(self, monkeypatch, discover):
        from contextlib import contextmanager

        @contextmanager
        def fake_db(*_a, **_k):
            class _Db:
                taxonomy = object()

            yield _Db()

        monkeypatch.setattr("nexus.commands.taxonomy_cmd._T2Database", fake_db)
        monkeypatch.setattr("nexus.commands.taxonomy_cmd._command_shared_t2_client", lambda: None)
        monkeypatch.setattr("nexus.commands.taxonomy_cmd._claude_available", lambda: False)
        monkeypatch.setattr("nexus.commands.taxonomy_cmd._default_db_path", lambda: "unused")
        monkeypatch.setattr("nexus.context.generate_context_l1", lambda *a, **k: None)
        monkeypatch.setattr("nexus.commands.taxonomy_cmd._run_discover_projection", lambda *a, **k: None)
        monkeypatch.setattr("nexus.db.make_t3", lambda: object())
        monkeypatch.setattr("nexus.config.load_config", lambda: {"taxonomy": {"auto_label": False}})
        monkeypatch.setattr("nexus.commands.taxonomy_cmd.discover_for_collection", discover)

    def test_discovers_exactly_the_target_collection(self, monkeypatch):
        from nexus.commands.collection import _run_taxonomy_discover

        seen = []
        self._wire(monkeypatch, lambda name, store, t3, *, force=False: seen.append((name, force)) or 3)
        assert _run_taxonomy_discover("docs__t") is True
        assert seen == [("docs__t", False)]

    def test_a_failure_is_told_with_the_retry_command_and_not_raised(self, monkeypatch, capsys):
        from nexus.commands.collection import _run_taxonomy_discover

        def boom(name, store, t3, *, force=False):
            raise RuntimeError("no vectors")

        self._wire(monkeypatch, boom)
        assert _run_taxonomy_discover("docs__t") is False
        assert "nx taxonomy discover --collection docs__t" in capsys.readouterr().err


class TestCrossModelOnAKeptTombstonedSource:
    SRC2, TGT2 = "docs__1-1__bge-base-en-v15-768__v1", "docs__1-1__voyage-3__v1"

    def _run(self, runner, jobs):
        cat = FakeCatalog([_row(self.SRC2, superseded_by=self.TGT2)], {"docs": "voyage-3"})
        return _invoke(runner, _engine(jobs=jobs), cat, [self.SRC2, "--to", "voyage-3"])

    def test_it_points_at_all_for_the_reentry(self, runner):
        jobs = [_job(JOB, source=self.SRC2, target=self.TGT2, state="done", disposition="kept_tombstoned")]
        result = self._run(runner, jobs)
        assert result.exit_code != 0
        assert "nx collection re-embed --all" in result.output and "tombstoned" in result.output

    def test_any_other_superseded_source_keeps_the_plain_refusal(self, runner):
        jobs = [_job(JOB, source=self.SRC2, target=self.TGT2, state="done", disposition="kept_by_flag")]
        result = self._run(runner, jobs)
        assert result.exit_code != 0
        assert "not a live, current collection" in result.output and "--all" not in result.output
