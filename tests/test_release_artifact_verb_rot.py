# SPDX-License-Identifier: AGPL-3.0-or-later
"""A release-only artifact must never name an `nx` verb that no longer exists.

Instance of nexus-1e2eh ("release-only procedures rot silently"). A step that
runs ONLY at release/cut time — a skill's runnable command, an e2e rehearsal
script, a workflow's ``run:`` step — is exercised by a human (or a rare
scheduled job) once per cut, so a verb deleted from the Click CLI can sit
inside one of these artifacts for a long time before anything notices. Three
concrete incidents landed in a single day (2026-07-24): RDR-155 P4b deleted
``nx guided-upgrade`` / ``migrate-to-service`` / ``storage migrate all``, and
left the ``engine-release`` skill prescribing ``--guided`` for one full cut,
plus two CI workflows (``guided-upgrade-mvv.yml``, and the ``run.sh --guided``
leg they drive) dead by construction.

``tests/test_engine_release_skill_parity.py`` mechanizes ONE instance of this
class in detail: the ``engine-release`` skill's flags against
``migration-rehearsal/run.sh``'s arg-parse loop, both directions (a
prescribed-but-dead flag, and a live journey the skill never learned about).
This module generalises the FORWARD half (a prescribed verb must be live) to
every release-only surface that actually burned us, resolved against the
REAL, LIVE Click command tree — never a hand-maintained list, which would
just be the same rot at one remove.

Surfaces swept (evidence-scoped, not speculative — see module-level
``_SKILL_GLOB`` / ``_SH_GLOBS`` / ``_WORKFLOW_GLOB``):

  * ``.claude/skills/*/SKILL.md``       — runnable ``nx`` commands in FENCED
    code blocks only. Prose legitimately narrates retired verbs while
    explaining the retirement (see the engine-release skill's own history
    section); only what an operator would copy-paste and run is in scope.
  * ``tests/e2e/**/*.sh`` + ``service/native-smoke.sh`` — every non-comment
    line. ``native-smoke.sh`` lives outside ``tests/e2e`` but is the exact
    incident-1 script (RDR-piwya.11 / v0.1.53) and is release-workflow-only,
    so it is swept explicitly alongside the glob.
  * ``.github/workflows/*.yml`` — only the ``run:`` step bodies (parsed via
    PyYAML, not text-matched), so job/step ``name:`` fields — plain
    documentation, not executed shell — never enter scope.

WHAT THIS DELIBERATELY DOES NOT COVER (precision over recall — a guard that
fires on fine code gets reflexively blessed and stops working):

  * Depth is capped at TWO tokens after ``nx`` (``nx <verb> <subverb>``). A
    rot instance exactly at the third level (e.g. ``daemon service
    install-binary`` renaming just ``install-binary``) is invisible to this
    sweep. Verifying deeper paths reliably needs to distinguish a subcommand
    token from a positional argument value, which gets exponentially more
    ambiguous with depth; two levels covers every incident this bead names.
  * Any ``nx`` mention split across a line continuation (a trailing
    backslash) is not reassembled. Not observed in the corpus; documented as
    a gap.
  * Python source comments/docstrings (e.g. ``commands/upgrade.py`` narrating
    ``nx guided-upgrade``'s history) are out of scope — this bead is about
    RELEASE-ONLY artifacts, not general source prose. That surface has its
    own pin: ``tests/upgrade/test_verb_demotion.py``.

THE ALLOWLIST MECHANISM (handles the retired-on-purpose case):

Two tables, both requiring a REASON string per entry, both checked for
staleness by ``test_allowlists_are_not_stale``:

  * ``_RETIRED_SCRIPT_ALLOWLIST`` (whole-file): for a file where EVERY
    invocation of a since-deleted verb is providably unreachable — the
    script self-guards on that verb's ``--help`` exit code before ever
    calling it (``rehearse_guided.sh`` / ``rehearse_cold.sh`` /
    ``rehearse_hole_punch.sh``), or the one live rehearsal (``rehearse.sh``)
    wraps its dead Phase B in exactly that guard, or the harness dispatcher
    (``run.sh``) only NAMES the deleted verb while explaining why its flag
    now refuses. These are RDR-155 P4b's own historical debris — real,
    currently-still-true rot that nexus-8nlj4 owns deleting or repointing —
    not a false positive of this sweep.
  * ``_FILE_VERB_ALLOWLIST`` (single verb within one file): the general
    escape valve for a future single-line exemption that does not warrant
    silencing an entire file. Empty today; kept wired and tested so the next
    maintainer has a mechanism instead of reaching for a broader regex
    exclusion or deleting the assertion.

Regex noise (``_GLOBAL_NOISE_ALLOWLIST``) is a THIRD, narrower table: tokens
the extractor captures immediately after ``nx`` that are ordinary English
prose, never a plausible subcommand candidate at all (``nx installed``, ``nx
version:``, ``the nx plugin``). These are extractor artifacts, not retired
verbs, so they are excluded at extraction time rather than treated as
tombstones — but still named, reasoned, and covered by the same staleness
check, so a maintainer sees exactly why each is excluded rather than a silent
regex tweak.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import click
import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent

_SKILL_GLOB = ".claude/skills/*/SKILL.md"
_SH_GLOBS = ("tests/e2e/**/*.sh",)
_EXTRA_SH_FILES = ("service/native-smoke.sh",)
_WORKFLOW_GLOB = ".github/workflows/*.yml"

#: ``nx <verb> [<subverb>]`` — both tokens required to start with a lowercase
#: letter, which is what keeps this from ever matching a flag (``--help``),
#: a shell variable (``$NX_BIN``), or a quoted argument (``"$FOO"``): none of
#: those start with ``[a-z]`` immediately after the required whitespace.
_VERB_RE = re.compile(r"\bnx\s+([a-z][a-z0-9-]*)(?:\s+([a-z][a-z0-9-]*))?")

#: Fenced ```bash / ```sh blocks in a skill markdown file — the only place a
#: skill PRESCRIBES a runnable command (mirrors
#: test_engine_release_skill_parity.py's ``_FENCE_RE``).
_FENCE_RE = re.compile(r"```(?:bash|sh)?\n(.*?)```", re.S)

# ── Allowlists (every entry requires a reason) ──────────────────────────────

#: verb-token -> reason. Tokens the regex captures directly after "nx" that
#: read as ordinary English, never a plausible CLI subcommand. Global (not
#: file-scoped): none of these could ever be genuine rot regardless of which
#: file they appear in — nobody writes "nx installed" intending to invoke a
#: subcommand named "installed".
_GLOBAL_NOISE_ALLOWLIST: dict[str, str] = {
    "installed": "prose, e.g. ok \"nx installed ($(nx --version))\" — describes tool state, not an invocation",
    "version": "prose, e.g. \"nx version: $(nx --version)\" — labels the --version output, not a subcommand",
    "plugin": "prose, e.g. \"...OLD nx plugin still installed\" — the Claude Code plugin named nx, not a CLI call",
    "thought": "historical note: 'nx thought' was removed 2026-02-26; the citing scenario is itself skip()-ped",
    "invocation": "prose, e.g. \"every top-level nx invocation is recorded\" — names the audit mechanism, not a verb",
}

#: relative-path -> reason. EVERY nx-verb invocation in the file is exempted.
#: Reserved for files where the ENTIRE set of findings stems from the same
#: self-guard or historical-notice property — see module docstring.
_RETIRED_SCRIPT_ALLOWLIST: dict[str, str] = {
    "tests/e2e/migration-rehearsal/rehearse_guided.sh": (
        "Self-guarded: 'if ! nx guided-upgrade --help ...; then echo RETIRED; exit 2; fi' "
        "at the top of the file exits before any real use of guided-upgrade below can run. "
        "guided-upgrade was deleted by RDR-155 P4b (7e47c285); nexus-8nlj4 owns deleting or "
        "repointing this file."
    ),
    "tests/e2e/migration-rehearsal/rehearse_cold.sh": (
        "Same top-of-file self-guard as rehearse_guided.sh ('if ! nx guided-upgrade --help "
        "...; then RETIRED; exit 2; fi'); every use below (including its own guided-upgrade "
        "calls) is unreachable. RDR-155 P4b; nexus-8nlj4."
    ),
    "tests/e2e/migration-rehearsal/rehearse_hole_punch.sh": (
        "Same top-of-file self-guard as rehearse_guided.sh; also unreachably invokes "
        "'nx storage migrate all' (the storage group was deleted the same RDR-155 P4b "
        "commit). Both are dead code behind the guided-upgrade preflight. nexus-8nlj4."
    ),
    "tests/e2e/migration-rehearsal/rehearse.sh": (
        "Phase B is internally guarded ('if nx migrate-to-service --help ...; then <use it> "
        "else echo RETIRED fi', ~line 270); migrate-to-service was deleted by RDR-155 P4b, so "
        "Phase B is a dead branch. Phases A/D/E (the daily-driver gate) are unaffected. "
        "nexus-8nlj4 tracks removing dead Phase B."
    ),
    "tests/e2e/migration-rehearsal/run.sh": (
        "Two lines only NAME the deleted CLI verb 'nx guided-upgrade' while documenting why "
        "its own --guided flag now refuses (an inline comment + the RETIRED echo message "
        "itself). run.sh's own flag retirement is independently verified by "
        "test_engine_release_skill_parity.py's forward/retired-flag checks."
    ),
}

#: (relative-path, verb-as-captured) -> reason. General-purpose single-verb
#: exemption for a file that is NOT otherwise a wholesale historical
#: tombstone. Empty today — see module docstring for why it stays wired.
_FILE_VERB_ALLOWLIST: dict[tuple[str, str], str] = {}


@dataclass(frozen=True)
class Invocation:
    file: str
    verb: str  # "tok1" or "tok1 tok2"
    tok1: str
    line: str


def _click_tree() -> dict[str, click.Command]:
    from nexus.cli import main  # noqa: PLC0415 — import at call time, not collection time

    return dict(main.commands)


def _verb_exists(tok1: str, tok2: str | None, tree: dict[str, click.Command]) -> bool:
    """True if ``nx <tok1> [<tok2>]`` resolves in the LIVE Click command tree.

    Depth-capped at two tokens by design — see module docstring. A leaf
    command (not a ``click.Group``) never has ``tok2`` checked: for a leaf,
    the second captured token is virtually always a positional argument
    value (``nx search "some query"``, ``nx tier-status developer``), never
    a subcommand, and treating it as one would be the exact false-positive
    class this module exists to avoid.
    """
    cmd = tree.get(tok1)
    if cmd is None:
        return False
    if isinstance(cmd, click.Group) and tok2 is not None:
        return tok2 in cmd.commands
    return True


def _scan_lines(text: str, *, file_label: str) -> list[Invocation]:
    """Every ``nx <verb> [<subverb>]`` candidate on a non-comment line.

    A line is a comment (and skipped) only when its FIRST non-whitespace
    character is ``#`` — a deliberately narrow definition. It does NOT strip
    trailing inline comments, so an invocation and an inline comment on the
    same line are both seen; in every file this sweep covers, a real
    invocation never shares a line with a false-positive-only inline
    comment, so this stays simple rather than hand-rolling a shell tokenizer.
    """
    found: list[Invocation] = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("#"):
            continue
        for m in _VERB_RE.finditer(raw_line):
            tok1, tok2 = m.group(1), m.group(2)
            if tok1 in _GLOBAL_NOISE_ALLOWLIST:
                continue
            verb = tok1 if tok2 is None else f"{tok1} {tok2}"
            found.append(Invocation(file=file_label, verb=verb, tok1=tok1, line=stripped[:160]))
    return found


def _extract_sh(path: Path) -> list[Invocation]:
    rel = str(path.relative_to(REPO_ROOT))
    return _scan_lines(path.read_text(encoding="utf-8"), file_label=rel)


def _extract_skill_md(path: Path) -> list[Invocation]:
    """Fenced ```bash/```sh blocks only — see module docstring."""
    rel = str(path.relative_to(REPO_ROOT))
    text = path.read_text(encoding="utf-8")
    fenced = "\n".join(_FENCE_RE.findall(text))
    return _scan_lines(fenced, file_label=rel)


def _extract_workflow(path: Path) -> list[Invocation]:
    """Only ``jobs.*.steps[].run`` bodies — never ``name:`` fields or YAML
    comments, both of which are documentation, not executed shell."""
    rel = str(path.relative_to(REPO_ROOT))
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    run_bodies: list[str] = []
    for job in (data.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            if isinstance(step, dict) and isinstance(step.get("run"), str):
                run_bodies.append(step["run"])
    return _scan_lines("\n".join(run_bodies), file_label=rel)


def _sh_files() -> list[Path]:
    paths: list[Path] = []
    for pattern in _SH_GLOBS:
        paths.extend(sorted(REPO_ROOT.glob(pattern)))
    paths.extend(REPO_ROOT / p for p in _EXTRA_SH_FILES)
    return paths


def _skill_files() -> list[Path]:
    return sorted(REPO_ROOT.glob(_SKILL_GLOB))


def _workflow_files() -> list[Path]:
    return sorted(REPO_ROOT.glob(_WORKFLOW_GLOB))


def _all_invocations() -> list[Invocation]:
    out: list[Invocation] = []
    for p in _sh_files():
        out.extend(_extract_sh(p))
    for p in _skill_files():
        out.extend(_extract_skill_md(p))
    for p in _workflow_files():
        out.extend(_extract_workflow(p))
    return out


# ── Non-vacuity ──────────────────────────────────────────────────────────────
#
# "The single most likely way this guard dies is a regex that quietly stops
# matching." Each assert below targets exactly that: a glob that resolves to
# no files (a moved directory), or an extractor that suddenly finds nothing
# in a file hand-verified to contain real invocations (a broken regex).

#: file -> minimum invocation count, hand-verified against the tree at
#: authoring time (2026-07-25). Deliberately well under the observed counts
#: so incidental doc trimming does not make this flaky, while a regex
#: regression (which drops counts to zero, not by a few) is still caught.
_ANCHOR_MIN_COUNTS: dict[str, int] = {
    ".claude/skills/engine-release/SKILL.md": 1,
    ".github/workflows/engine-service-release.yml": 2,
    "service/native-smoke.sh": 1,
    "tests/e2e/release-sandbox.sh": 25,
    "tests/e2e/upgrade-shakeout.sh": 15,
    "tests/e2e/migration-rehearsal/rehearse_era_hop.sh": 15,
    # Anchors INSIDE the whole-file allowlist too: proves the extractor
    # still sees real content in these files, not just that the allowlist
    # is silencing an empty scan.
    "tests/e2e/migration-rehearsal/rehearse.sh": 20,
    "tests/e2e/migration-rehearsal/rehearse_guided.sh": 5,
}


def test_globs_resolve_to_files() -> None:
    assert len(_sh_files()) >= 30, f"tests/e2e/**/*.sh glob looks broken: {len(_sh_files())} files"
    assert len(_skill_files()) >= 2, f".claude/skills/*/SKILL.md glob looks broken: {_skill_files()}"
    assert len(_workflow_files()) >= 8, f".github/workflows/*.yml glob looks broken: {_workflow_files()}"
    for extra in _EXTRA_SH_FILES:
        assert (REPO_ROOT / extra).is_file(), f"explicitly-swept file moved: {extra}"


def test_click_tree_is_not_vacuous() -> None:
    tree = _click_tree()
    assert len(tree) >= 25, f"live Click tree looks broken: {sorted(tree)}"
    for verb in ("init", "doctor", "upgrade", "search", "store", "daemon", "collection", "hooks"):
        assert verb in tree, f"expected top-level verb missing from live tree: {verb}"


def test_extraction_is_not_vacuous_in_aggregate() -> None:
    total = len(_all_invocations())
    assert total >= 250, (
        f"only {total} nx-invocations extracted across every swept surface — "
        "the extraction regex likely broke (last known-good baseline: 355)"
    )


@pytest.mark.parametrize("relpath,minimum", sorted(_ANCHOR_MIN_COUNTS.items()))
def test_anchor_file_extraction_is_not_vacuous(relpath: str, minimum: int) -> None:
    """A hand-verified-nonzero file that suddenly yields zero (or far fewer)
    invocations means the regex broke, not that the file went quiet."""
    path = REPO_ROOT / relpath
    assert path.is_file(), f"anchor file moved: {relpath}"
    if relpath.endswith(".sh"):
        found = _extract_sh(path)
    elif relpath.endswith(".md"):
        found = _extract_skill_md(path)
    else:
        found = _extract_workflow(path)
    assert len(found) >= minimum, (
        f"{relpath}: extractor found only {len(found)} invocation(s), expected >= {minimum}. "
        f"Found: {[i.verb for i in found]}"
    )


def test_verb_resolution_correctly_rejects_known_dead_verbs() -> None:
    """Pins the resolver's negative case against the REAL tree, independent
    of any file scan: these verbs were deleted by RDR-155 P4b and must never
    resolve, or the resolver itself (not just the extraction) is broken."""
    tree = _click_tree()
    for tok1, tok2 in (("guided-upgrade", None), ("migrate-to-service", None),
                       ("migration-audit", None), ("storage", "migrate")):
        assert not _verb_exists(tok1, tok2, tree), f"nx {tok1} {tok2 or ''} unexpectedly resolved"


def test_verb_resolution_correctly_accepts_known_live_verbs() -> None:
    tree = _click_tree()
    for tok1, tok2 in (("init", None), ("doctor", None), ("upgrade", None),
                       ("daemon", "service"), ("store", "put"), ("collection", "prune"),
                       ("hooks", "update")):
        assert _verb_exists(tok1, tok2, tree), f"nx {tok1} {tok2 or ''} unexpectedly failed to resolve"


def test_allowlists_are_not_stale() -> None:
    """Every allowlist entry must carry a non-empty reason, and every
    file-keyed entry must point at a file that still exists — an allowlist
    entry for a deleted file is silently protecting nothing."""
    for verb, reason in _GLOBAL_NOISE_ALLOWLIST.items():
        assert reason.strip(), f"_GLOBAL_NOISE_ALLOWLIST[{verb!r}] has no reason"
    for relpath, reason in _RETIRED_SCRIPT_ALLOWLIST.items():
        assert reason.strip(), f"_RETIRED_SCRIPT_ALLOWLIST[{relpath!r}] has no reason"
        assert (REPO_ROOT / relpath).is_file(), (
            f"_RETIRED_SCRIPT_ALLOWLIST names a file that no longer exists: {relpath}. "
            "Remove the stale entry."
        )
    for (relpath, verb), reason in _FILE_VERB_ALLOWLIST.items():
        assert reason.strip(), f"_FILE_VERB_ALLOWLIST[({relpath!r}, {verb!r})] has no reason"
        assert (REPO_ROOT / relpath).is_file(), (
            f"_FILE_VERB_ALLOWLIST names a file that no longer exists: {relpath} ({verb}). "
            "Remove the stale entry."
        )


# ── The guard itself ─────────────────────────────────────────────────────────


def test_no_release_artifact_names_a_dead_verb() -> None:
    """A release-only artifact (skill fenced command, e2e rehearsal script,
    workflow run: step) must never name an `nx` verb the live Click CLI does
    not have.

    A hit here means one of:
      1. A verb genuinely rotted — the artifact was not swept when the verb
         was renamed/deleted. Fix the artifact.
      2. The verb is INTENTIONALLY retired and the artifact is an
         acknowledged tombstone (a self-guarded historical script, a
         retirement notice). Add an entry to `_RETIRED_SCRIPT_ALLOWLIST`
         (whole file) or `_FILE_VERB_ALLOWLIST` (single verb) above, with a
         REASON citing what guards it or why it is kept.
      3. The extractor mis-parsed ordinary prose as a verb. Add the false
         token to `_GLOBAL_NOISE_ALLOWLIST` with a reason — but check twice,
         since this is also how a real hit gets waved through.
    """
    tree = _click_tree()
    offenders: list[Invocation] = []
    for inv in _all_invocations():
        if inv.file in _RETIRED_SCRIPT_ALLOWLIST:
            continue
        if (inv.file, inv.verb) in _FILE_VERB_ALLOWLIST:
            continue
        tok1, _, tok2 = inv.verb.partition(" ")
        if not _verb_exists(tok1, tok2 or None, tree):
            offenders.append(inv)

    assert not offenders, (
        "release-only artifact(s) name an nx verb the live CLI does not have:\n"
        + "\n".join(f"  {o.file}: nx {o.verb!r} — {o.line!r}" for o in offenders)
        + "\n\nSee this module's docstring / test_no_release_artifact_names_a_dead_verb's "
        "own docstring for the three ways to resolve a hit (fix the artifact, allowlist an "
        "acknowledged tombstone, or allowlist extractor noise)."
    )


# ── Mutation-verify ──────────────────────────────────────────────────────────
#
# Proves the guard actually distinguishes a live verb from a missing one,
# using the resolver directly (no source-file mutation, no git side effects —
# the physical-deletion exercise this bead also requires is performed
# manually once per the bead's own instructions and reported out-of-band, not
# re-run on every CI invocation).


def test_mutation_a_removed_top_level_verb_is_detected() -> None:
    tree = _click_tree()
    assert _verb_exists("init", None, tree)
    mutated = dict(tree)
    del mutated["init"]
    assert not _verb_exists("init", None, mutated), "resolver did not notice the removed verb"
    # Restored view (a fresh call) proves the mutation was local to this test.
    assert _verb_exists("init", None, _click_tree())


def test_mutation_a_removed_subcommand_is_detected() -> None:
    tree = _click_tree()
    assert _verb_exists("daemon", "service", tree)
    daemon_group = tree["daemon"]
    assert isinstance(daemon_group, click.Group)
    original_commands = dict(daemon_group.commands)
    del daemon_group.commands["service"]
    try:
        assert not _verb_exists("daemon", "service", tree), (
            "resolver did not notice the removed subcommand"
        )
    finally:
        daemon_group.commands.clear()
        daemon_group.commands.update(original_commands)
    assert _verb_exists("daemon", "service", _click_tree())
