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
``_SKILL_GLOB`` / ``_SH_GLOBS`` / ``_WORKFLOW_GLOB`` / ``_HOOKS_JSON_FILE`` /
``_README_FILE``):

  * ``.claude/skills/*/SKILL.md``       — runnable ``nx`` commands in FENCED
    code blocks only. Prose legitimately narrates retired verbs while
    explaining the retirement (see the engine-release skill's own history
    section — ``nx guided-upgrade`` is named there in plain prose, not a
    fenced block, precisely so this sweep leaves it alone); only what an
    operator would copy-paste and run is in scope.
  * ``tests/e2e/**/*.sh`` + ``scripts/*.sh`` + ``conexus/hooks/scripts/*.sh``
    + ``service/native-smoke.sh`` — every non-comment line. ``native-smoke.sh``
    lives outside ``tests/e2e`` but is the exact incident-1 script
    (RDR-piwya.11 / v0.1.53) and is release-workflow-only, so it is swept
    explicitly alongside the glob. ``scripts/*.sh`` is top-level only
    (nexus-zmfan) — deliberately NOT recursive, so ``scripts/rdr152-sandbox/``
    and ``scripts/validate/`` stay out of charter (personal/ops sandbox
    tooling, not a release-only artifact; a real `nx storage migrate` rot
    found there in scope-widening reconnaissance is tracked as its own bead,
    not silently swept in here). ``conexus/hooks/scripts/*.sh`` WAS the
    shipped plugin hook scripts every plugin user's Claude Code session
    executes — until RDR-215 bead nexus-q02nx.21 ported twelve of the
    thirteen into the wheel and deleted them. That glob still resolves, but
    to ONE file (``_run_python_hook.sh``) carrying ZERO nx invocations, so
    it is an inert surface today, kept wired for whatever shell hook lands
    next rather than because it currently proves anything. The shipped-hook
    nx-invocation signal it used to carry now lives entirely in
    ``conexus/hooks/hooks.json``, whose anchor was raised from 1 to 3 to
    match (see ``_ANCHOR_MIN_COUNTS``).

    The ported hooks themselves are NOT swept: they are Python in
    ``src/nexus/hooks/``, and this module reads .sh, skill markdown,
    workflow YAML, hooks.json and the plugin README. That is a real
    coverage reduction, disclosed rather than papered over — a dead ``nx``
    verb named inside a ported hook's Python is invisible here. It is also
    much less exposed than the bash was: a verb named in Python is a string
    in a module the unit suite imports, not an un-run line in a shell
    script nobody executes between releases, which is the whole premise of
    this module (nexus-1e2eh, "release-only procedures rot silently").
  * ``conexus/hooks/hooks.json`` — every entry's ``command`` PLUS its own
    ``args``, joined back into the line the entry stands for, however deeply
    nested (parsed via ``json.loads``, not text-matched). This is the
    strongest instance of the class this bead generalises: a SHIPPED
    artifact executing a deleted verb at runtime, on every session start,
    for every plugin user, silenced by ``|| true`` (nexus-i711w Stage 2
    sub-stage B's own incident).

    Two things about this surface changed at RDR-215 bead nexus-q02nx.22 and
    both had to, together. The file no longer names ``nx`` anywhere: its
    entries are exec form, so the verb lives in ``args`` and a
    ``command``-only walk sees a bare ``nx-hook`` with nothing after it
    (measured against the post-bead file with the pre-bead extractor: ZERO
    invocations, in a file carrying six). And the verbs it names now belong
    to a DIFFERENT vocabulary — ``nx-hook``'s flat
    ``nexus._hook_runtime.entry.VERB_TABLE``, not the Click tree — where an
    unregistered name exits 2 with a diagnostic on every SessionStart, which
    is the same incident shape at a different address. So the extractor joins
    ``args`` (``_extract_hooks_json``) and the resolver routes by tool
    (``_HOOK_VERB_RE`` / ``_hook_verb_exists``). The alternative on the table
    was deleting this anchor, on .21's own precedent; it was rejected because
    the coverage had not become meaningless, only invisible, and an anchor
    removed for that reason removes the evidence rather than the problem.
  * ``conexus/README.md`` — fenced code blocks (as with skills) PLUS inline
    single-backtick code spans. Unlike a skill's history section (prose
    narration, no backticks), the README's own nexus-i711w incident was an
    inline-backtick mention — `` `nx daemon t2 ensure-running --quiet` `` —
    embedded in ordinary prose, invisible to a fenced-only sweep. Scoped to
    this one file, not ``conexus/**/*.md`` generally: the wider skill/agent
    doc corpus carries the same retrospective-narration risk fenced-only
    protects against, and Hal's greenlight covers this bead's named surfaces,
    not an open-ended docs sweep (docs/ itself stays deliberately uncovered
    per the withdrawn 2026-07-28 widening, precedent below).
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
    calling it (``rehearse_cold.sh`` / ``rehearse_hole_punch.sh``), or the
    one live rehearsal (``rehearse.sh``)
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
#: nexus-zmfan: ``scripts/*.sh`` is deliberately top-level only (not
#: ``scripts/**/*.sh``) — see the module docstring's surfaces-swept note.
#: ``conexus/hooks/scripts/*.sh`` are the shipped plugin hook scripts.
_SH_GLOBS = ("tests/e2e/**/*.sh", "scripts/*.sh", "conexus/hooks/scripts/*.sh")
_EXTRA_SH_FILES = ("service/native-smoke.sh",)
_WORKFLOW_GLOB = ".github/workflows/*.yml"
#: nexus-zmfan: the shipped plugin's hooks manifest — every ``command``
#: string, wherever nested, is in scope (see module docstring).
_HOOKS_JSON_FILE = "conexus/hooks/hooks.json"
#: nexus-zmfan: scoped to this one file, not a ``conexus/**/*.md`` sweep —
#: see the module docstring's surfaces-swept note for why.
_README_FILE = "conexus/README.md"

#: ``nx <verb> [<subverb>]`` — both tokens required to start with a lowercase
#: letter, which is what keeps this from ever matching a flag (``--help``),
#: a shell variable (``$NX_BIN``), or a quoted argument (``"$FOO"``): none of
#: those start with ``[a-z]`` immediately after the required whitespace.
_VERB_RE = re.compile(r"\bnx\s+([a-z][a-z0-9-]*)(?:\s+([a-z][a-z0-9-]*))?")

#: ``nx-hook <verb>`` — the SECOND console script this module resolves against
#: (RDR-215 bead nexus-q02nx.22). ``_VERB_RE`` cannot see these: it requires
#: whitespace immediately after ``nx``, and ``nx-hook`` has a hyphen there.
#:
#: This exists because RDR-215 MOVED the rot risk rather than removing it. The
#: hooks this module used to watch as ``nx <verb>`` in a shipped manifest are
#: now ``nx-hook <verb>`` in the same shipped manifest, dispatched through
#: ``nexus._hook_runtime.entry.VERB_TABLE``; an unregistered verb there exits 2
#: with a diagnostic on every SessionStart, for every plugin user — the exact
#: nexus-i711w shape this module's hooks.json surface was added for. Teaching
#: the extractor the new spelling was chosen over removing the hooks.json
#: anchor precisely because the anchor's own coverage had not become
#: meaningless, only invisible. A number that improves after a refactor is two
#: claims, one about the code and one about the instrument (T2
#: ``nexus_rdr/215-gates-that-lost-their-domain``).
#:
#: Underscores are in the character class, unlike ``_VERB_RE``: the RDR-184
#: ledger verbs are spelled ``expectations_census`` and friends.
#: Depth is ONE token — ``nx-hook`` has a flat verb table, no subcommands.
_HOOK_VERB_RE = re.compile(r"\bnx-hook\s+([a-z][a-z0-9_-]*)")

#: Fenced ```bash / ```sh blocks in a skill markdown file — the only place a
#: skill PRESCRIBES a runnable command (mirrors
#: test_engine_release_skill_parity.py's ``_FENCE_RE``).
_FENCE_RE = re.compile(r"```(?:bash|sh)?\n(.*?)```", re.S)

#: Inline single-backtick code spans (`` `nx foo bar` ``) — README.md only
#: (nexus-zmfan). A skill's retrospective narration wraps a retired verb in
#: plain prose with no backticks (see the module docstring); README mixes
#: narration with literal copy-paste snippets inside inline code, which is
#: exactly the shape of the nexus-i711w incident this closes.
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")

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
    "is": "prose, e.g. \"the installed nx is ${INSTALLED_VERSION}\" (scripts/reinstall-tool.sh) — a copula, not a verb",
    "console": (
        "prose, e.g. \"this generation predates the nx-hook console script\" "
        "(tests/e2e/post-publish-dispatch-check.sh's _prereq_fail message) — "
        "the English noun, and the same line's \"nx-hook is not on PATH\" is "
        "already covered by the \"is\" entry above"
    ),
}

#: relative-path -> reason. EVERY nx-verb invocation in the file is exempted.
#: Reserved for files where the ENTIRE set of findings stems from the same
#: self-guard or historical-notice property — see module docstring.
_RETIRED_SCRIPT_ALLOWLIST: dict[str, str] = {
    "tests/e2e/migration-rehearsal/rehearse_cold.sh": (
        "Self-guarded: 'if ! nx guided-upgrade --help ...; then echo RETIRED; exit 2; fi' "
        "at the top of the file exits before any real use of guided-upgrade below can run "
        "(including its own guided-upgrade calls). guided-upgrade was deleted by RDR-155 P4b "
        "(7e47c285); nexus-8nlj4 owns deleting or repointing this file."
    ),
    "tests/e2e/migration-rehearsal/rehearse_hole_punch.sh": (
        "Same top-of-file self-guard as rehearse_cold.sh; also unreachably invokes "
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
_FILE_VERB_ALLOWLIST: dict[tuple[str, str], str] = {
    # nexus-x8fuq: upgrade-shakeout's demoted-verb detector
    # (_check_no_demoted_verb) exists to FAIL when doctor output names a
    # retired verb, and its self-test plants exactly that string as the RED
    # fixture. These are detector test-fixtures quoting the dead verb on
    # purpose — the opposite of rot: deleting them would blind the detector
    # test to the very class this sweep protects against.
    ("tests/e2e/upgrade-shakeout.sh", "guided-upgrade"): (
        "planted RED fixture for the demoted-verb detector's self-test"
    ),
    ("tests/e2e/upgrade-shakeout.sh", "guided-upgrade to"): (
        "planted RED fixture text for _check_no_demoted_verb's self-test"
    ),
    # REMOVED at RDR-215 bead nexus-q02nx.21: the nexus-fgekf entry keyed on
    # ("conexus/hooks/scripts/pre_close_verification_hook.sh", "binary is").
    # It exempted extractor noise — the close gate's capability-gap warning
    # says "the nx binary is absent, or 'nx scratch list' failed", and the
    # extractor read the prose after "nx" as a verb. That script is deleted;
    # the gate is `nexus.hooks.pre_close_verification`, which this sweep does
    # not scan (its surfaces are .sh / skill md / workflow yml / hooks.json /
    # the plugin README — never Python in the wheel). The warning text still
    # exists there, so the exemption did not become unnecessary, it became
    # unreachable — and `test_allowlist_entries_are_not_stale` asserts every
    # key resolves to a real file, so leaving it would fail on the path, not
    # on the verb.
}


@dataclass(frozen=True)
class Invocation:
    file: str
    verb: str  # "tok1" or "tok1 tok2"
    tok1: str
    line: str
    #: Which console script's vocabulary ``verb`` belongs to: ``"nx"``
    #: (resolved against the live Click tree) or ``"nx-hook"`` (resolved
    #: against the live ``VERB_TABLE``). Defaulted so every existing
    #: construction site stays an ``nx`` invocation without restating it.
    tool: str = "nx"


def _click_tree() -> dict[str, click.Command]:
    from nexus.cli import main  # noqa: PLC0415 — import at call time, not collection time

    return dict(main.commands)


def _hook_verb_table() -> dict[str, str]:
    """The LIVE ``nx-hook`` verb table — same discipline as :func:`_click_tree`.

    Imported at call time, never at collection time, and never transcribed into
    a list here: a hand-maintained copy would be the same rot one level up,
    which is this module's whole premise.
    """
    from nexus._hook_runtime.entry import VERB_TABLE  # noqa: PLC0415 — import at call time

    return dict(VERB_TABLE)


def _hook_verb_exists(verb: str, table: dict[str, str]) -> bool:
    """True if ``nx-hook <verb>`` resolves in the live dispatch table.

    Flat, unlike :func:`_verb_exists`: ``nx-hook`` is a hand-rolled dispatcher
    over a single dict, deliberately not a Click group, so there is no second
    level to cap.
    """
    return verb in table


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
        for m in _HOOK_VERB_RE.finditer(raw_line):
            verb = m.group(1)
            if verb in _GLOBAL_NOISE_ALLOWLIST:
                continue
            found.append(
                Invocation(
                    file=file_label, verb=verb, tok1=verb, line=stripped[:160], tool="nx-hook"
                )
            )
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


def _extract_hooks_json(path: Path) -> list[Invocation]:
    """Every executable invocation in the shipped hooks manifest, however
    deeply nested — mirrors the workflow extractor's discipline of scanning
    only what actually EXECUTES (never a ``matcher:``/documentation field).

    **``command`` alone is not the invocation any more.** Every entry named a
    verb inside its ``command`` STRING until RDR-215; exec form splits it, so
    ``nx-hook session-start`` is stored as ``{"command": "nx-hook", "args":
    ["session-start"]}`` and a ``command``-only walk sees the bare word
    ``nx-hook`` with no verb after it — nothing to match, and the file's
    extraction count silently goes to zero while every entry in it is still
    perfectly capable of naming a verb that does not exist.

    So each entry is reassembled into the line it stands for, ``command``
    followed by its own ``args``, and THAT is what gets scanned. Without this
    join, :data:`_HOOK_VERB_RE` above would match nothing here and the whole
    nx-hook half of this module would be green and blind — which is the defect
    class bead nexus-q02nx.21 found seven instances of and this bead is
    explicitly watching for.
    """
    return _extract_hooks_json_from(path, file_label=str(path.relative_to(REPO_ROOT)))


def _extract_hooks_json_from(path: Path, *, file_label: str) -> list[Invocation]:
    """:func:`_extract_hooks_json`'s body, against an arbitrary path and label.

    Split out so the mutation test can run the REAL extractor over a perturbed
    COPY of the manifest without writing to the repo — the same reason the
    Click mutations below mutate a dict rather than a source file. The split is
    a parameter, not a second copy: there is one walk.
    """
    import json  # noqa: PLC0415 — call-site import, this module's only JSON consumer

    data = json.loads(path.read_text(encoding="utf-8"))
    lines: list[str] = []

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            command = node.get("command")
            if isinstance(command, str):
                args = [a for a in (node.get("args") or []) if isinstance(a, str)]
                lines.append(" ".join([command, *args]))
            for key, value in node.items():
                if key not in ("command", "args"):
                    _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(data)
    return _scan_lines("\n".join(lines), file_label=file_label)


def _extract_readme_md(path: Path) -> list[Invocation]:
    """``conexus/README.md``: fenced code blocks (as with skills) PLUS
    inline single-backtick code spans — see the module docstring and
    ``_INLINE_CODE_RE`` for why README needs the wider net a skill does not.
    """
    rel = str(path.relative_to(REPO_ROOT))
    text = path.read_text(encoding="utf-8")
    fenced = "\n".join(_FENCE_RE.findall(text))
    inline = "\n".join(_INLINE_CODE_RE.findall(text))
    return _scan_lines(fenced, file_label=rel) + _scan_lines(inline, file_label=rel)


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
    out.extend(_extract_hooks_json(REPO_ROOT / _HOOKS_JSON_FILE))
    out.extend(_extract_readme_md(REPO_ROOT / _README_FILE))
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
    # nexus-zmfan widening (hand-verified 2026-08-07):
    # scripts/reinstall-tool.sh was an anchor here (2 invocations) until
    # nexus-utpuw.8. Its `nx daemon service start` / `nx mineru start` /
    # `nx daemon aspect-worker start` calls existed to restart daemons the
    # script had just killed to clear the venv for an in-place swap. Installs
    # are side-by-side now, nothing is killed, and those calls are gone -- so
    # the file genuinely contains no nx verb invocation and there is nowhere
    # to retarget the anchor to. Removed rather than lowered to 0: an anchor
    # asserting ">= 0 invocations" proves nothing about the extractor, which
    # is the one thing an anchor is for. The remaining 11 anchors still do.
    # Raised 1 -> 3 at RDR-215 bead nexus-q02nx.21 (measured then: 4
    # invocations — `nx upgrade`, `nx self install`, `nx self gc`,
    # `nx hook session-start`). hooks.json is the ONLY carrier of the
    # shipped-plugin invocation signal this module watches, so a token floor
    # of 1 is not enough: see the `conexus/hooks/scripts/*.sh` note below for
    # what it replaced.
    #
    # 3 -> 4 at bead nexus-q02nx.22, and the four invocations it was measured
    # against are GONE — that bead converted every one of them to `nx-hook`
    # exec form, which `_VERB_RE` cannot match (hyphen where it needs
    # whitespace) and which puts the verb in `args` where a `command`-only
    # walk never looks. Measured with the pre-bead extractor against the
    # post-bead file: ZERO. The anchor would have caught that, and the
    # temptation was to answer it by deleting the anchor.
    #
    # It was answered the other way instead: `_HOOK_VERB_RE` plus the
    # command+args join in `_extract_hooks_json`, so the same six entries are
    # resolved against the live `VERB_TABLE`. Measured after: 6
    # (`upgrade-auto`, `preflight`, `self-gc`, `session-start`,
    # `session-context`, `rdr`). Floor set to 4, the same deliberate slack
    # under the observed count this dict uses everywhere else.
    "conexus/hooks/hooks.json": 4,
    # REMOVED at RDR-215 bead nexus-q02nx.21: the
    # "conexus/hooks/scripts/subagent-start.sh": 3 anchor. That script (and
    # eleven siblings) were ported into the wheel and deleted, and the whole
    # `conexus/hooks/scripts/*.sh` glob now resolves to ONE surviving file,
    # `_run_python_hook.sh`, which contains ZERO nx invocations (measured).
    # So there is nowhere in that surface to retarget the anchor to.
    #
    # Removed rather than lowered to 0, on this dict's own reinstall-tool.sh
    # precedent above: an anchor asserting ">= 0 invocations" proves nothing
    # about the extractor, which is the one thing an anchor is for.
    "conexus/README.md": 5,
}


def test_globs_resolve_to_files() -> None:
    assert len(_sh_files()) >= 35, f"the .sh globs look broken: {len(_sh_files())} files"
    assert len(_skill_files()) >= 2, f".claude/skills/*/SKILL.md glob looks broken: {_skill_files()}"
    assert len(_workflow_files()) >= 8, f".github/workflows/*.yml glob looks broken: {_workflow_files()}"
    for extra in _EXTRA_SH_FILES:
        assert (REPO_ROOT / extra).is_file(), f"explicitly-swept file moved: {extra}"
    assert (REPO_ROOT / _HOOKS_JSON_FILE).is_file(), f"hooks manifest moved: {_HOOKS_JSON_FILE}"
    assert (REPO_ROOT / _README_FILE).is_file(), f"plugin README moved: {_README_FILE}"


def test_click_tree_is_not_vacuous() -> None:
    tree = _click_tree()
    assert len(tree) >= 25, f"live Click tree looks broken: {sorted(tree)}"
    for verb in ("init", "doctor", "upgrade", "search", "store", "daemon", "collection", "hooks"):
        assert verb in tree, f"expected top-level verb missing from live tree: {verb}"


def test_extraction_is_not_vacuous_in_aggregate() -> None:
    total = len(_all_invocations())
    assert total >= 330, (
        f"only {total} nx-invocations extracted across every swept surface — "
        "the extraction regex likely broke (measured 611 at RDR-215 bead "
        "nexus-q02nx.22, of which 10 are `nx-hook`; the 376 recorded at the "
        "nexus-zmfan widening was against a smaller tree, not a drop since)"
    )


@pytest.mark.parametrize("relpath,minimum", sorted(_ANCHOR_MIN_COUNTS.items()))
def test_anchor_file_extraction_is_not_vacuous(relpath: str, minimum: int) -> None:
    """A hand-verified-nonzero file that suddenly yields zero (or far fewer)
    invocations means the regex broke, not that the file went quiet."""
    path = REPO_ROOT / relpath
    assert path.is_file(), f"anchor file moved: {relpath}"
    if relpath == _README_FILE:
        found = _extract_readme_md(path)
    elif relpath == _HOOKS_JSON_FILE:
        found = _extract_hooks_json(path)
    elif relpath.endswith(".sh"):
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


#: Whole-file allowlist entries whose REASON asserts a specific code pattern.
#: The claim is what earns the exemption, so the claim is what must be checked.
#: Maps relpath -> a substring that MUST still appear in that file.
#:
#: Review finding (2026-07-25): the staleness check verified an entry's SHAPE
#: (reason non-empty, file exists) and never its TRUTH. Every retired-script
#: entry here is exempted because the script SELF-GUARDS on the deleted verb's
#: --help exit code before any real use. Nothing asserted that guard clause was
#: still present. Delete it in an unrelated edit and the whole-file exemption
#: keeps silencing every invocation in that file forever, with no test noticing
#: -- an allowlist entry outliving its justification, which is precisely how
#: the /links/orphaned census exclusion went stale the same day.
_REASON_CLAIMS: dict[str, str] = {
    # Each reason asserts the script self-guards on the deleted verb's --help
    # exit code, making every later invocation unreachable. Assert the guard.
    "tests/e2e/migration-rehearsal/rehearse_cold.sh": "nx guided-upgrade --help",
    "tests/e2e/migration-rehearsal/rehearse_hole_punch.sh": "--help",
    # "Phase B is internally guarded ('if nx migrate-to-service --help ...')".
    "tests/e2e/migration-rehearsal/rehearse.sh": "nx migrate-to-service --help",
    # "Two lines only NAME the deleted verb ... the RETIRED echo message
    # itself". The claim is that the mention is a retirement NOTICE, not a use,
    # so assert the notice is still what is there.
    "tests/e2e/migration-rehearsal/run.sh": "RETIRED",
}


def test_allowlist_reasons_are_still_TRUE_not_merely_present() -> None:
    """A reason that names a guard must be checkable against the guard.

    This is the half the original staleness check was missing. Shape-checking a
    justification string proves the string exists, never that it is still
    accurate -- and an inaccurate justification is exactly what a silently
    over-broad exemption looks like.
    """
    # Non-vacuity: if the claims map drifts out of sync with the allowlist it
    # is meant to police, this check quietly stops covering entries.
    unpoliced = sorted(set(_RETIRED_SCRIPT_ALLOWLIST) - set(_REASON_CLAIMS))
    assert not unpoliced, (
        f"_RETIRED_SCRIPT_ALLOWLIST entries with no verifiable claim in "
        f"_REASON_CLAIMS: {unpoliced}. Either add the code pattern its reason "
        f"asserts, or say explicitly why the reason is not mechanically "
        f"checkable. An unchecked reason is an exemption on trust."
    )

    for relpath, must_contain in _REASON_CLAIMS.items():
        path = REPO_ROOT / relpath
        if not path.is_file():
            continue  # covered by test_allowlists_are_not_stale
        body = path.read_text(encoding="utf-8", errors="replace")
        assert must_contain in body, (
            f"{relpath} is whole-file allowlisted because its reason claims it "
            f"self-guards, but {must_contain!r} is NO LONGER IN THE FILE. The "
            f"exemption is now silencing invocations that may be genuinely "
            f"reachable.\nFIX: restore the guard, or remove the allowlist entry "
            f"so the sweep checks the file again."
        )


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
    hook_table = _hook_verb_table()
    offenders: list[Invocation] = []
    for inv in _all_invocations():
        if inv.file in _RETIRED_SCRIPT_ALLOWLIST:
            continue
        if (inv.file, inv.verb) in _FILE_VERB_ALLOWLIST:
            continue
        if inv.tool == "nx-hook":
            if not _hook_verb_exists(inv.verb, hook_table):
                offenders.append(inv)
            continue
        tok1, _, tok2 = inv.verb.partition(" ")
        if not _verb_exists(tok1, tok2 or None, tree):
            offenders.append(inv)

    assert not offenders, (
        "release-only artifact(s) name an nx/nx-hook verb the live CLI does not have:\n"
        + "\n".join(f"  {o.file}: {o.tool} {o.verb!r} — {o.line!r}" for o in offenders)
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


def test_mutation_a_removed_nx_hook_verb_is_detected() -> None:
    """The nx-hook resolver's negative case, against the real VERB_TABLE."""
    table = _hook_verb_table()
    assert _hook_verb_exists("session-start", table)
    mutated = dict(table)
    del mutated["session-start"]
    assert not _hook_verb_exists("session-start", mutated), (
        "resolver did not notice the removed nx-hook verb"
    )
    assert _hook_verb_exists("session-start", _hook_verb_table())


def test_mutation_an_unregistered_hooks_json_verb_is_caught_end_to_end(tmp_path) -> None:
    """The WHOLE nx-hook path, exercised on a perturbed copy of the manifest.

    The resolver mutation above proves the lookup; this proves everything in
    front of it — the command+args join, ``_HOOK_VERB_RE``, the tool routing
    in the guard — by renaming a real verb to one that is not registered and
    requiring the sweep to say so. Without this, every nx-hook assertion in
    this module could be green because it examined nothing, which is the
    failure mode the whole bead is watching for.
    """
    import json  # noqa: PLC0415 — mirrors _extract_hooks_json's call-site import

    real = REPO_ROOT / _HOOKS_JSON_FILE
    data = json.loads(real.read_text(encoding="utf-8"))

    renamed = 0

    def _walk(node: object) -> None:
        nonlocal renamed
        if isinstance(node, dict):
            if node.get("command") == "nx-hook" and isinstance(node.get("args"), list):
                node["args"] = ["definitely-not-a-registered-verb"]
                renamed += 1
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(data)
    assert renamed >= 1, (
        "no nx-hook entry found in the real hooks.json to perturb — this "
        "mutation test is no longer exercising anything"
    )

    perturbed = tmp_path / "hooks.json"
    perturbed.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")

    found = _extract_hooks_json_from(perturbed, file_label=_HOOKS_JSON_FILE)
    assert len(found) >= renamed, (
        f"the extractor found {len(found)} invocation(s) in the perturbed "
        f"manifest, expected at least the {renamed} it rewrote"
    )
    table = _hook_verb_table()
    dead = [i for i in found if i.tool == "nx-hook" and not _hook_verb_exists(i.verb, table)]
    assert len(dead) == renamed, (
        f"the sweep flagged {len(dead)} unregistered verb(s), expected {renamed}. "
        "The extraction or the routing is not reaching hooks.json's exec-form entries."
    )

    # And the unperturbed file is clean, so the assertion above is a real
    # discrimination rather than something that fires either way.
    clean = _extract_hooks_json(real)
    assert clean, "the real manifest yielded no invocations"
    assert not [i for i in clean if i.tool == "nx-hook" and not _hook_verb_exists(i.verb, table)]


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
