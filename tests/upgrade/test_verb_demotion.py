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

import ast
from pathlib import Path

import pytest
from click.testing import CliRunner

import nexus
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


# ── the everyday surfaces never ADVERTISE a demoted verb ────────────────────
#
# P4.2 (nexus-n7u38.29) found the hole this section closes. The pins above
# check --help VISIBILITY, and passed while `nx upgrade` and `nx doctor` both
# printed "Run: nx guided-upgrade" from the nexus-0rwwv bridge — a hidden
# verb advertised as the everyday remedy. Hiding a verb from --help while the
# product's own output tells you to run it is not demotion.

#: The everyday user-facing upgrade surfaces (the ones a user actually runs)
#: plus the health module that renders doctor's checks and the hooks module
#: that speaks at SessionStart.
#:
#: `hooks.py` was MISSING from this list until the P4.R2 harvest, and the
#: census passed vacuously for it the whole time: `session_start` was still
#: telling every Claude Code session to run `nx guided-upgrade` — the most
#: everyday surface of the lot, and pinned green by its own test. A census is
#: only as honest as its inventory, which is exactly the failure mode this
#: module exists to catch. When a new surface starts speaking to users about
#: upgrade, it belongs here.
_EVERYDAY_SURFACE_MODULES = (
    "commands/upgrade.py",
    "commands/doctor.py",
    "health.py",
    "hooks.py",
)

#: Where a demoted verb name may legitimately appear in a printed string.
#: EMPTY, deliberately — and an empty allowlist is a real claim, not an
#: oversight: there is no everyday-output case for naming a demoted verb.
#: Genuine remedies name reachable verbs (`endpoint_failure_migration_hint`
#: points at `nx upgrade`; the block path prints the rollback flag, which is
#: reachable). Adding an entry here means arguing that a user should be told
#: to run something they cannot find in --help.
_ADVERTISEMENT_ALLOWLIST: frozenset[tuple[str, str]] = frozenset()


def _printed_strings(source: str) -> list[str]:
    """Every string constant in *source* that is not a docstring.

    Comments never enter the AST, so prose ABOUT the demoted verbs (this
    module's own history, the retirement rationale at each former call site)
    is exempt by construction, while any string that could reach a user is
    in scope.
    """
    tree = ast.parse(source)
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = getattr(node, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def _advertisements(text: str) -> list[str]:
    """Demoted verbs *advertised as an invocation* in *text*.

    The predicate is `nx <verb>`, not the bare verb name: "migration" is
    also an ordinary English word and a module path (`nexus.migration_jobs`,
    `migration-reports`, "Re-run the failed store migrations"). Telling a
    user to RUN something is the failure mode; talking about migration is
    not.
    """
    return [verb for verb in DEMOTED_TOP_LEVEL if f"nx {verb}" in text]


@pytest.mark.parametrize("module", _EVERYDAY_SURFACE_MODULES)
def test_everyday_surfaces_never_advertise_a_demoted_verb(module: str) -> None:
    path = Path(nexus.__file__).parent / module
    offenders = [
        (verb, text)
        for text in _printed_strings(path.read_text())
        for verb in _advertisements(text)
        if (module, verb) not in _ADVERTISEMENT_ALLOWLIST
    ]
    assert not offenders, (
        f"{module} tells the user to run demoted verb(s) {[v for v, _ in offenders]} "
        f"in: {[t for _, t in offenders]!r} — a verb hidden from --help must not be "
        "advertised as the remedy. The everyday remedy is `nx upgrade`."
    )


def test_the_advertisement_census_is_not_vacuous() -> None:
    """The pin above passes trivially if `_printed_strings` returns nothing,
    `_advertisements` matches nothing, or the surface list rots. Prove each
    link still sees the violation it exists to catch."""
    # The exact shape P4.2 removed, and the shapes that must NOT trip it.
    assert _advertisements("Run: nx guided-upgrade") == ["guided-upgrade"]
    assert _advertisements("wrote migration-reports/") == []
    assert _advertisements("Re-run the failed store migrations:") == []

    source = "x = 'Run: nx guided-upgrade'\ny = 'plain'\n"
    assert _printed_strings(source) == ["Run: nx guided-upgrade", "plain"]
    # Docstrings — prose, never user output — stay out of scope.
    assert _printed_strings("'''Run: nx guided-upgrade.'''\nx = 'plain'\n") == ["plain"]

    for module in _EVERYDAY_SURFACE_MODULES:
        path = Path(nexus.__file__).parent / module
        assert path.exists(), f"{module} moved — the census is scanning nothing"
        assert _printed_strings(path.read_text()), f"{module} parsed to zero strings"


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
