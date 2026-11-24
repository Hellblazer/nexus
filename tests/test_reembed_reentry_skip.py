# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 Phase 3 (engine 0.1.202): ``nx collection re-embed --all`` and a re-entry the engine would refuse.

Engine 0.1.202 answers 409 ``EndRefused`` ({error, job_id}) to the re-entry of a kept_tombstoned job whose target is
retired or has no registry row. The client reads the target's row first and skips the re-entry with one line; the 409
that still arrives (the target retired between the read and the send) is the same skip, never exit 1. The fake engine
and catalog are ``tests/test_reembed_verb.py``'s.
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from nexus.db.http_vector_client import VectorServiceError
from tests.test_reembed_verb import (  # noqa: F401 — local_mode is an autouse fixture the module's tests rely on
    JOB, KEPT_SRC, KEPT_TGT, _engine, _invoke, _kept_tombstoned_world, local_mode,
)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class TestReentrySkip:
    @pytest.mark.parametrize(("target_row", "why"), [
        ("superseded", f"its target {KEPT_TGT} is retired"),
        (None, f"its target {KEPT_TGT} is not registered"),
    ])
    def test_a_reentry_whose_target_is_retired_or_gone_is_skipped_with_one_line_and_not_sent(
        self, runner, target_row, why,
    ):
        """Engine 0.1.202 refuses to re-enter a kept_tombstoned job whose target is gone or retired (409). The
        client reads the target's registry row first, says why it did not send the re-entry, and exits 0."""
        jobs, cat = _kept_tombstoned_world(target_row=target_row)
        db = _engine(jobs=jobs)
        result = _invoke(runner, db, cat, ["--all", "--no-dry-run", "--yes"])
        assert result.exit_code == 0, result.output
        db.reembed_reenter.assert_not_called()
        lines = [ln for ln in result.output.splitlines() if "not re-entered" in ln]
        assert len(lines) == 1 and lines[0].startswith(f"{KEPT_SRC}: not re-entered: {why}; "), result.output
        assert "re-entry of job" not in result.output, "it is not offered as a re-entry either"

    def test_the_skip_line_names_a_remedy_that_exists_for_each_kind_of_re_entry(self, runner):
        """A cutover re-entry holds no document (the job would only delete the source); a running one holds a live
        document the job would have moved. The remedies are commands that exist."""
        for live, total, kind, remedy in [
            (0, 0, "cutover", f"`nx collection delete {KEPT_SRC}`"),
            (1, 2, "running", f"`nx collection reindex {KEPT_SRC}`"),
        ]:
            jobs, cat = _kept_tombstoned_world(live=live, total=total, target_row="superseded")
            result = _invoke(runner, _engine(jobs=jobs), cat, ["--all"])
            (line,) = [ln for ln in result.output.splitlines() if "not re-entered" in ln]
            assert remedy in line, (kind, line)

    def test_a_dry_run_names_the_skip_not_a_reentry(self, runner):
        jobs, cat = _kept_tombstoned_world(target_row="superseded")
        db = _engine(jobs=jobs)
        result = _invoke(runner, db, cat, ["--all"])
        assert result.exit_code == 0, result.output
        assert f"{KEPT_SRC}: not re-entered: its target {KEPT_TGT} is retired; " in result.output
        assert "would be re-entered" not in result.output

    def test_a_skipped_reentry_is_still_a_name_skip_may_use(self, runner):
        jobs, cat = _kept_tombstoned_world(target_row="superseded")
        db = _engine(jobs=jobs)
        result = _invoke(runner, db, cat, ["--all", "--skip", KEPT_SRC])
        assert result.exit_code == 0, result.output
        assert "not one of --all's candidates" not in result.output

    def test_an_end_refused_409_from_the_engine_is_the_same_skip_not_a_failure(self, runner):
        """The race: the target was retired between the client's read and the engine's check. The engine answers
        409 ``EndRefused`` ({error, job_id}); that is a skip, never exit 1."""
        jobs, cat = _kept_tombstoned_world()
        db = _engine(jobs=jobs)
        db.reembed_reenter.side_effect = VectorServiceError(
            "HTTP 409", code=409, engine_body={
                "error": f"job {JOB} cannot be re-entered: its target {KEPT_TGT} is itself superseded by "
                         f"docs__x; the source is not re-entered",
                "job_id": JOB})
        result = _invoke(runner, db, cat, ["--all", "--no-dry-run", "--yes"])
        assert result.exit_code == 0, result.output
        db.reembed_reenter.assert_called_once_with(KEPT_SRC)
        lines = [ln for ln in result.output.splitlines() if "not re-entered" in ln]
        assert len(lines) == 1 and lines[0].startswith(f"{KEPT_SRC}: not re-entered: its target {KEPT_TGT}"), result.output
