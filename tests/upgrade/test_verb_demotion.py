# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-185 P4.1 (nexus-n7u38.28): the upgrade surface is ONE verb.

Gap 1 was that upgrading meant holding a command GRAPH whose applicable
verbs depended on your install's era. The ladder makes the graph
derivable, so the verbs whose ONLY job was upgrade-cycle work leave the
user-facing story — demoted to internal primitives (``hidden=True``:
still callable, still tested, out of ``--help`` and the docs), never
deleted (deletion of the migration module is RDR-155 P4b's job, a
standing blocker, and hiding keeps scripts/surgical use working).

The line is drawn by JOB, not by appearance in the old graph: a verb with
a genuine non-upgrade job keeps its surface (``nx collection reindex``
refreshes content; ``re-embed`` is a deliberate model choice;
``daemon restart-stale`` covers the aspect-worker/mineru population the
process precondition does not; ``init --service`` is a fresh install).
Deleting those because they once appeared in the upgrade graph would be
simplistic, not simple.

Genuine decisions stay reachable: ROLLBACK (``nx storage migrate vectors
--rollback``) can never be derived — the product cannot know you want to
undo — so it survives in the surgical storage group, printed as the
remedy exactly where it is needed (migrate_cmd.py's block path).
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.commands.collection import collection

#: Verbs whose ONLY job was upgrade-cycle work — the ladder now does it.
#: Hidden from --help; still callable (internal primitive).
DEMOTED_TOP_LEVEL = (
    "guided-upgrade",      # provision + migrate  -> preconditions + substrate rung
    "migrate-to-service",  # the T3 substrate ETL -> the substrate rung
    "migration",           # sentinel recovery    -> crash-recovery plumbing
    "migration-audit",     # diagnostic           -> nx doctor is the user surface
)

#: The ENTIRE user-facing upgrade story (RDR-185 Success Criterion).
USER_FACING_UPGRADE_SURFACE = ("upgrade", "doctor")


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _help(runner: CliRunner, *args: str) -> str:
    result = runner.invoke(main, [*args, "--help"])
    assert result.exit_code == 0, result.output
    return result.output


# ── the surface ──────────────────────────────────────────────────────────────


def test_the_upgrade_story_is_one_verb(runner: CliRunner) -> None:
    out = _help(runner)
    for verb in USER_FACING_UPGRADE_SURFACE:
        assert f"\n  {verb}" in out, f"{verb} must stay user-facing"


def test_demoted_verbs_are_not_in_the_user_facing_help(runner: CliRunner) -> None:
    out = _help(runner)
    for verb in DEMOTED_TOP_LEVEL:
        assert f"\n  {verb}" not in out, (
            f"{verb} still appears in `nx --help` — its job is the ladder's now"
        )


@pytest.mark.parametrize("verb", DEMOTED_TOP_LEVEL)
def test_demoted_verbs_remain_callable_as_internal_primitives(
    runner: CliRunner, verb: str
) -> None:
    """Demoted, NOT deleted: surgical/dev use and existing scripts keep
    working (and RDR-155 P4b owns the actual deletion)."""
    result = runner.invoke(main, [verb, "--help"])
    assert result.exit_code == 0
    assert "Usage:" in result.output


def test_collection_upgrade_era_repair_is_demoted(runner: CliRunner) -> None:
    out = _help(runner, "collection")
    assert "backfill-hash" not in out  # upgrade-era repair -> the ladder heals


def test_collection_verbs_with_a_real_job_stay(runner: CliRunner) -> None:
    """NOT simplistic: these appeared in the Gap-1 graph but their job is
    not upgrade — reindex refreshes changed content, re-embed is the user
    deliberately choosing a different model."""
    out = _help(runner, "collection")
    assert "reindex" in out
    assert "re-embed" in out


def test_hooks_update_all_is_demoted_but_install_stays(runner: CliRunner) -> None:
    """nx upgrade refreshes managed hooks itself (refresh_all_managed_hooks),
    so the manual sweep leaves the story; installing hooks in a new repo is
    a different job."""
    out = _help(runner, "hooks")
    assert "update-all" not in out
    assert "install" in out


def test_restart_stale_stays_user_facing(runner: CliRunner) -> None:
    """The process precondition cycles the STORAGE service; restart-stale
    covers the aspect-worker/mineru population it does not touch. A real
    job, kept."""
    out = _help(runner, "daemon")
    assert "restart-stale" in out


# ── the genuine decisions stay reachable ────────────────────────────────────


def test_rollback_stays_reachable(runner: CliRunner) -> None:
    """Undo is NEVER derivable — the product cannot know you want it. The
    rollback flag survives in the surgical storage group and the block path
    prints it exactly where it is needed."""
    out = _help(runner, "storage", "migrate", "vectors")
    assert "--rollback" in out


def test_rewrite_ids_was_never_built() -> None:
    """GH #1408 proposed `nx collection rewrite-ids`; RDR-185 retired the
    NEED for it (the correct id is a pure function of stored text, computed
    on the wire inside the rung). The right verb count for this job is
    zero — this pin keeps it that way."""
    assert "rewrite-ids" not in collection.commands


def test_no_upgrade_verb_grew_back(runner: CliRunner) -> None:
    """The Gap-1 tripwire: a NEW top-level verb whose name says 'upgrade' or
    'migrate' must be argued past this list, not appended silently."""
    out = _help(runner)
    listed = {
        line.strip().split()[0]
        for line in out.splitlines()
        if line.startswith("  ") and line.strip() and not line.strip().startswith("-")
    }
    upgrade_shaped = {
        v for v in listed
        if ("migrat" in v or "upgrade" in v) and v not in USER_FACING_UPGRADE_SURFACE
    }
    assert not upgrade_shaped, (
        f"new upgrade-shaped user-facing verb(s): {sorted(upgrade_shaped)} — "
        "the upgrade story is `nx upgrade` + `nx doctor`"
    )
