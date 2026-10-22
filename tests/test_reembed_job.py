# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-210 Phase 6 Step 3 (nexus-cnxjk): the client half of the re-embed job, as pure functions.

Model tokens are inert (``voyage-model-a``/``voyage-model-b``): the code under test reads only a token's
prefix, and a real model name in a test body trips the RDR-109 mode-declaration census.
"""
from __future__ import annotations

import time

import pytest

from nexus import reembed_job as rj

BGE = "bge-base-en-v15-768"
VA = "voyage-model-a"
VB = "voyage-model-b"


def _row(name, *, ct="docs", model=BGE, state="live", superseded_by="", owner="1.1"):
    return {
        "name": name, "content_type": ct, "owner_id": owner, "embedding_model": model,
        "lifecycle_state": state, "superseded_by": superseded_by,
    }


def _job(job_id, source, target, state, disposition=None):
    return {
        "job_id": job_id, "source_collection": source, "target_collection": target,
        "state": state, "disposition": disposition,
    }


class TestFamilyAndCap:
    def test_family_is_the_prefix(self):
        assert rj.model_family(VA) == "voyage"
        assert rj.model_family(BGE) == "bge"
        assert rj.model_family("minilm-l6-v2-384") is None

    def test_voyage_target_gets_the_default_cap_or_the_flag(self):
        assert rj.effective_token_cap(VA, None) == 10_000_000
        assert rj.effective_token_cap(VA, 5) == 5

    def test_bge_target_has_no_cap_even_when_a_flag_is_given(self):
        assert rj.effective_token_cap(BGE, None) is None
        assert rj.effective_token_cap(BGE, 5) is None


class TestTargetName:
    def test_is_built_from_the_row_columns_with_dots_in_the_owner_read_as_hyphens(self):
        assert rj.target_collection_name("docs", "1.87", VA) == f"docs__1-87__{VA}__v1"

    def test_a_slug_owner_is_kept(self):
        assert rj.target_collection_name("knowledge", "notes", VB) == f"knowledge__notes__{VB}__v1"

    def test_result_is_a_conformant_name(self):
        from nexus.corpus import is_conformant_collection_name

        assert is_conformant_collection_name(rj.target_collection_name("knowledge", "1.1", VB))

    @pytest.mark.parametrize("owner", [None, "", "   "])
    def test_a_null_or_blank_owner_is_refused_and_never_rendered_as_none(self, owner):
        with pytest.raises(rj.TargetNameError) as raised:
            rj.target_collection_name("docs", owner, VA)
        assert "no owner_id" in str(raised.value) and "None" not in str(raised.value)

    def test_the_default_placeholder_and_the_rows_tenant_id_are_refused(self):
        with pytest.raises(rj.TargetNameError, match="placeholder 'default'"):
            rj.target_collection_name("docs", "default", VA)
        with pytest.raises(rj.TargetNameError, match="placeholder 'tenant-7'"):
            rj.target_collection_name("docs", "tenant-7", VA, tenant_ids=(None, "tenant-7"))
        # the same value is a fine owner when it is not this row's tenant
        assert rj.target_collection_name("docs", "tenant-7", VA, tenant_ids=("other",)) == f"docs__tenant-7__{VA}__v1"

    @pytest.mark.parametrize("owner", ["a b", "x__y", "a/b"])
    def test_an_owner_that_makes_no_conformant_name_is_refused(self, owner):
        with pytest.raises(rj.TargetNameError, match="conformant"):
            rj.target_collection_name("docs", owner, VA)


class TestMissingPiece:
    def test_served_model_is_no_problem(self):
        assert rj.missing_piece(VA, [BGE, VA]) is None

    def test_unserved_voyage_names_the_key_and_the_restart(self):
        text = rj.missing_piece(VA, [BGE])
        assert text and VA in text and "bge-base-en-v15-768" in text
        assert "voyage_api_key" in text and "nx daemon service stop && nx daemon service start" in text

    def test_unserved_bge_names_the_model_files(self):
        text = rj.missing_piece(BGE, [VA])
        assert text and "nx init --service" in text and "bge model files" in text

    def test_empty_served_list_says_no_model(self):
        assert "no model" in (rj.missing_piece(BGE, []) or "")


class TestStaleRows:
    PROFILE = {"docs": VA, "code": VB}

    def test_only_live_unsuperseded_rows_off_the_profile(self):
        rows = [
            _row("docs__a", model=BGE),                                  # stale
            _row("docs__b", model=VA),                                   # on profile
            _row("docs__c", model=BGE, state="quarantine"),              # never a candidate
            _row("docs__d", model=BGE, state="dormant"),
            _row("docs__e", model=BGE, state="disputed"),
            _row("docs__f", model=BGE, superseded_by="docs__x"),         # superseded: only as a re-entry
            _row("code__g", ct="code", model=VA),                        # stale against code's model
            _row("rdr__h", ct="rdr", model=BGE),                         # no profile model for rdr: not judged
        ]
        assert [r["name"] for r in rj.stale_rows(rows, self.PROFILE)] == ["code__g", "docs__a"]


class TestReentryCandidacy:
    def _rows(self, **by_name):
        return {n: _row(n, superseded_by=t) for n, t in by_name.items()}

    def test_kept_tombstoned_source_with_no_document_row_is_a_cutover_reentry(self):
        jobs = [_job("j1", "docs__s", "docs__t", "done", "kept_tombstoned")]
        found = rj.reentry_candidates(jobs, self._rows(docs__s="docs__t"), lambda n: 0, lambda n: 0)
        assert [(r.job["job_id"], r.kind) for r in found] == [("j1", "cutover")]

    def test_restored_live_document_makes_it_a_running_reentry(self):
        jobs = [_job("j1", "docs__s", "docs__t", "done", "kept_tombstoned")]
        found = rj.reentry_candidates(jobs, self._rows(docs__s="docs__t"), lambda n: 1, lambda n: 2)
        assert [(r.job["job_id"], r.kind) for r in found] == [("j1", "running")]

    def test_tombstones_still_there_and_none_live_is_not_yet(self):
        jobs = [_job("j1", "docs__s", "docs__t", "done", "kept_tombstoned")]
        assert rj.reentry_candidates(jobs, self._rows(docs__s="docs__t"), lambda n: 0, lambda n: 3) == []

    @pytest.mark.parametrize("disposition", ["kept_by_flag", "kept_residue", "deleted", "revived"])
    def test_other_dispositions_are_never_listed(self, disposition):
        jobs = [_job("j1", "docs__s", "docs__t", "done", disposition)]
        assert rj.reentry_candidates(jobs, self._rows(docs__s="docs__t"), lambda n: 1, lambda n: 1) == []

    def test_a_source_not_superseded_by_the_jobs_target_is_not_listed(self):
        jobs = [_job("j1", "docs__s", "docs__t", "done", "kept_tombstoned")]
        assert rj.reentry_candidates(jobs, self._rows(docs__s="docs__other"), lambda n: 0, lambda n: 0) == []
        assert rj.reentry_candidates(jobs, {"docs__s": _row("docs__s")}, lambda n: 0, lambda n: 0) == []
        assert rj.reentry_candidates(jobs, {}, lambda n: 0, lambda n: 0) == []

    def test_a_job_holding_the_source_lock_suppresses_it(self):
        jobs = [
            _job("j2", "docs__s", "docs__t2", "failed"),
            _job("j1", "docs__s", "docs__t", "done", "kept_tombstoned"),
        ]
        assert rj.reentry_candidates(jobs, self._rows(docs__s="docs__t"), lambda n: 0, lambda n: 0) == []

    def test_only_the_newest_job_of_a_source_counts(self):
        jobs = [  # newest first, as the status route lists them
            _job("j2", "docs__s", "docs__t", "done", "kept_by_flag"),
            _job("j1", "docs__s", "docs__t", "done", "kept_tombstoned"),
        ]
        assert rj.reentry_candidates(jobs, self._rows(docs__s="docs__t"), lambda n: 0, lambda n: 0) == []


class TestCandidates:
    def test_a_stale_source_a_job_holds_is_named_not_planned(self):
        rows = [_row("docs__a"), _row("docs__b")]
        jobs = [_job("j9", "docs__a", "docs__ta", "paused")]
        found = rj.candidates(rows, {"docs": VA}, jobs, lambda n: 0, lambda n: 0)
        assert [r["name"] for r in found.stale] == ["docs__b"]
        assert [(r["name"], j["job_id"]) for r, j in found.held] == [("docs__a", "j9")]

    def test_a_finished_job_does_not_hold_its_source(self):
        rows = [_row("docs__a")]
        jobs = [_job("j9", "docs__a", "docs__ta", "done_with_skips")]
        found = rj.candidates(rows, {"docs": VA}, jobs, lambda n: 0, lambda n: 0)
        assert [r["name"] for r in found.stale] == ["docs__a"] and found.held == []


class _Reader:
    def __init__(self, rows, profile):
        self._rows, self._profile = rows, profile

    def list_collections(self):
        return self._rows

    def embedding_profile(self):
        return [{"content_type": ct, "embedding_model": m, "dimension": 3} for ct, m in self._profile.items()]


class TestOffProfileHint:
    def test_hint_when_a_live_collection_is_off_the_profile(self):
        reader = _Reader([_row("docs__a", model=BGE)], {"docs": VA})
        assert rj.off_profile_hint(lambda: reader) == rj.REEMBED_HINT
        assert "nx collection re-embed --all" in rj.REEMBED_HINT

    def test_no_hint_when_every_live_collection_is_on_the_profile(self):
        reader = _Reader([_row("docs__a", model=VA), _row("docs__b", model=BGE, state="quarantine")], {"docs": VA})
        assert rj.off_profile_hint(lambda: reader) is None

    def test_a_superseded_off_profile_row_is_no_reason_for_the_hint(self):
        reader = _Reader([_row("docs__a", model=BGE, superseded_by="docs__t")], {"docs": VA})
        assert rj.off_profile_hint(lambda: reader, jobs_reader=lambda: []) is None

    def test_no_hint_without_an_engine_profile(self):
        assert rj.off_profile_hint(lambda: _Reader([_row("docs__a")], {})) is None

    def test_an_unreadable_catalog_is_no_hint_and_no_error(self):
        def boom():
            raise RuntimeError("engine down")

        assert rj.off_profile_hint(boom) is None

    def test_a_hung_engine_does_not_hold_the_caller(self, monkeypatch):
        monkeypatch.setattr(rj, "HINT_READ_TIMEOUT_S", 0.2)

        def hang():
            time.sleep(5)

        started = time.monotonic()
        assert rj.off_profile_hint(hang) is None
        assert time.monotonic() - started < 2
