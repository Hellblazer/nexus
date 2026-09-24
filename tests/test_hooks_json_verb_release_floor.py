# SPDX-License-Identifier: AGPL-3.0-or-later
"""``hooks.json``'s ``nx-hook`` verbs must resolve in the PREVIOUS RELEASED
wheel, not merely this dev tree (nexus-t9klx, the 7.58.0 release blocker).

``tests/test_release_artifact_verb_rot.py`` already resolves ``nx-hook
<verb>`` invocations in ``conexus/hooks/hooks.json`` against the LIVE
:data:`nexus._hook_runtime.entry.VERB_TABLE` -- but that table is imported
from THIS checkout, which always carries whatever ``hooks.json`` itself
carries, because the two are edited in the same commits on the same branch.
That check can never see the defect this module exists for: the conexus
PLUGIN marketplace ships ``hooks.json`` the moment it is merged, while the
``conexus`` PyPI package -- the thing that installs ``nx-hook`` and
populates ``VERB_TABLE`` on a user's machine -- ships separately, later, on
its own tag-cut cadence. A verb newly wired into ``hooks.json`` is live for
every plugin user's *next* Claude Code session; it is NOT live for the `nx`
CLI they still have installed until they upgrade, which itself runs through
the version-lockstep hook.

Measured 2026-09-2x: seven ``hooks.json`` entries on develop named
``nx-hook`` verbs (``behaviour-census``, ``version-lockstep``,
``mcp-connect-wait``, ``subagent-git-write-gate``,
``phase-review-close-gate``, ``mailbox-drain``, ``mcp-connect-check``) that
the released ``v7.57.0`` wheel's ``VERB_TABLE`` did not register --
``src/nexus/_hook_runtime/entry.py``'s ``main()`` exits 2 on an unknown
verb in that release, so every ``UserPromptSubmit`` and every
``PreToolUse`` on ``Bash`` refused outright for a session still on the
7.57.0 CLI with the 7.58.0 plugin pinned. Worse, the ONE thing that
upgrades the installed CLI -- the version-lockstep hook itself -- was one
of the seven, so nothing could self-heal.

**The rule this module checks, precisely.** "Previous released" means: the
newest ``v<major>.<minor>.<patch>`` tag reachable from ``HEAD`` (``git tag
--merged HEAD``) whose parsed version is ``<=`` ``pyproject.toml``'s own
version. A single ``<=`` covers both real shapes a checkout is in, with no
branch-specific carve-out:

  * On ``develop``, ``pyproject.toml`` names the version that was JUST
    released (``7.57.0``) -- nothing has bumped it for the next cycle yet.
    The newest tag ``<=`` that version is ``v7.57.0`` itself: an exact
    match, because the currently-installed CLI everywhere IS that tag.
  * On a release branch, ``pyproject.toml`` has already been bumped to the
    NEXT version pre-tag (``7.58.0``), and no tag names it yet. The newest
    tag ``<=`` that version is ``v7.57.0`` -- one release behind, which is
    exactly the CLI every existing install still has until the new tag
    ships.

A strictly-below rule would reject the ``develop`` case (no tag is
strictly below its own version's exact match); an equal-only rule would
reject the release-branch case (no tag equals a version that has not been
cut yet). ``<=`` is both at once.

**No qualifying tag is a FAILURE, never a skip.** A depth-1, tagless clone
(a shallow CI checkout with no ``fetch-tags``) cannot read git history and
would silently pass every ``hooks.json`` entry through if this returned
"nothing to check" instead of failing -- which is indistinguishable from
"verified clean" to anyone reading the CI result. See
:func:`_previous_released_tag`'s failure message for what to do about it.

**Why VERB_TABLE is read via ``git show`` + AST, never ``importlib``.**
Importing ``nexus._hook_runtime.entry`` at an old tag is not something
Python supports (this process is running the CURRENT tree's interpreter
against the CURRENT tree's ``sys.path``); ``git show <tag>:<path>`` reads
the tag's bytes without checking anything out, and :mod:`ast` parses that
text without executing it -- so this test needs no worktree, no
``pip install``, and never accidentally imports the wrong module because
``sys.modules`` already cached today's ``nexus._hook_runtime.entry`` under
that name.
"""
from __future__ import annotations

import ast
import json
import re
import subprocess
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS_JSON = REPO_ROOT / "conexus" / "hooks" / "hooks.json"
PYPROJECT = REPO_ROOT / "pyproject.toml"
ENTRY_PY_RELPATH = "src/nexus/_hook_runtime/entry.py"

#: Exact three-part release tags only -- never ``plugin-vX.Y.Z-N`` or
#: ``engine-service-vX.Y.Z``, both of which this pattern's anchors exclude
#: by construction (extra non-digit text after the patch number).
_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")

Version = tuple[int, int, int]


def _run_git(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
    )


def _current_version() -> Version:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    raw = data["project"]["version"]
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", raw)
    assert m, f"pyproject.toml's version does not parse as semver: {raw!r}"
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _is_shallow() -> bool:
    proc = _run_git(["rev-parse", "--is-shallow-repository"])
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def _previous_released_tag() -> str:
    """The newest ``v<major>.<minor>.<patch>`` tag reachable from HEAD whose
    parsed version is ``<=`` pyproject.toml's version. See the module
    docstring's "The rule this module checks, precisely" section.

    FAILS LOUD (never skips) when no such tag is visible: a tagless/shallow
    checkout is not evidence the CLI/plugin pairing is safe, it is evidence
    this check cannot see the answer, and skip-passing that would be
    exactly the vacuous-gate failure mode CLAUDE.md's "gates fail loud on
    absent dependencies" rule exists to name.
    """
    current = _current_version()
    proc = _run_git(["tag", "--merged", "HEAD"])
    if proc.returncode != 0:
        pytest.fail(
            f"git tag --merged HEAD failed (rc {proc.returncode}): {proc.stderr.strip()}"
        )
    candidates: list[tuple[Version, str]] = []
    for line in proc.stdout.splitlines():
        tag = line.strip()
        m = _TAG_RE.match(tag)
        if not m:
            continue
        version = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if version <= current:
            candidates.append((version, tag))
    if not candidates:
        log_proc = _run_git(["log", "--oneline"])
        depth = len(log_proc.stdout.splitlines()) if log_proc.returncode == 0 else -1
        pytest.fail(
            "no released v<major>.<minor>.<patch> tag <= pyproject.toml's version "
            f"({'.'.join(map(str, current))}) is reachable from HEAD via "
            f"`git tag --merged HEAD`. This checkout has {depth} commit(s) visible "
            f"from HEAD and is-shallow-repository={_is_shallow()}. Fetch full "
            "history and tags (`git fetch --unshallow --tags`) before trusting this "
            "check again -- a tagless checkout cannot see the answer, which is not "
            "the same thing as the answer being clean."
        )
    candidates.sort()
    return candidates[-1][1]


def _verb_table_at_tag(tag: str) -> set[str]:
    """``VERB_TABLE``'s string keys at *tag*, via ``git show`` + AST -- never
    ``importlib`` (see the module docstring's final section for why).

    Matches both ``VERB_TABLE = {...}`` (``ast.Assign``) and the annotated
    form actually used today, ``VERB_TABLE: dict[str, str] = {...}``
    (``ast.AnnAssign``), so this survives the annotation being added,
    removed, or changed.
    """
    proc = _run_git(["show", f"{tag}:{ENTRY_PY_RELPATH}"])
    if proc.returncode != 0:
        pytest.fail(
            f"git show {tag}:{ENTRY_PY_RELPATH} failed (rc {proc.returncode}): "
            f"{proc.stderr.strip()}"
        )
    tree = ast.parse(proc.stdout, filename=f"{tag}:{ENTRY_PY_RELPATH}")
    for node in ast.walk(tree):
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        if (
            isinstance(target, ast.Name)
            and target.id == "VERB_TABLE"
            and isinstance(value, ast.Dict)
        ):
            return {
                key.value
                for key in value.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
    pytest.fail(
        f"could not find a VERB_TABLE dict literal in {tag}:{ENTRY_PY_RELPATH} -- "
        "the AST walk is broken, or that tag's entry.py has no VERB_TABLE at all "
        "(too old to be a meaningful 'previous released' floor)."
    )


def _nx_hook_verbs_in_hooks_json(path: Path = HOOKS_JSON) -> list[tuple[str, str]]:
    """Every ``nx-hook <verb>`` invocation in *path*, as ``(verb,
    joined-line)`` pairs for diagnostics.

    *path* defaults to the real shipped manifest; a caller may point it at
    a tmp copy (e.g. a ``git show <other-ref>:conexus/hooks/hooks.json``
    dump) to check a different commit's manifest against the same floor
    without a detached checkout.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    found: list[tuple[str, str]] = []

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            if node.get("command") == "nx-hook":
                args = [a for a in (node.get("args") or []) if isinstance(a, str)]
                if args:
                    found.append((args[0], " ".join(["nx-hook", *args])))
            for key, value in node.items():
                if key not in ("command", "args"):
                    _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(data)
    return found


# ── Non-vacuity ──────────────────────────────────────────────────────────────


def test_previous_released_tag_resolves_to_a_real_tag() -> None:
    tag = _previous_released_tag()
    assert _TAG_RE.match(tag), f"resolved 'previous released tag' is not tag-shaped: {tag!r}"
    proc = _run_git(["rev-parse", "--verify", "--quiet", f"{tag}^{{commit}}"])
    assert proc.returncode == 0, f"{tag} does not resolve to a real commit"


def test_the_resolved_tags_verb_table_is_not_vacuous() -> None:
    tag = _previous_released_tag()
    table = _verb_table_at_tag(tag)
    assert len(table) >= 5, f"{tag}'s VERB_TABLE parsed to only {table!r} -- the AST walk is broken"
    # session-start is the FIRST verb this table ever carried (nexus-q02nx.5)
    # and every tag this module will ever resolve post-dates it.
    assert "session-start" in table, f"{tag}'s VERB_TABLE is missing session-start: {table!r}"


def test_hooks_json_extraction_is_not_vacuous() -> None:
    invocations = _nx_hook_verbs_in_hooks_json()
    assert invocations, (
        "conexus/hooks/hooks.json named zero nx-hook invocations -- the extractor "
        "broke, the manifest moved, or every nx-hook entry was deleted; this "
        "module's own hooks.json anchor (test_hooks_json_shape_lint.py) pins a "
        "nonzero nx-hook count too, so check that gate's own failure first."
    )


# ── Positive controls (mutation-verify) ─────────────────────────────────────
#
# The tests above prove the pipeline finds SOMETHING; they do not prove it
# discriminates a bad manifest from a clean one -- a detector that always
# reports zero offenders passes every assertion above just as well as a
# working one. These feed the REAL extractor + resolution logic a
# deliberately broken manifest via `_nx_hook_verbs_in_hooks_json`'s `path`
# parameter (critique on 69b6cac76/cfe9c923c) and require the offender to
# actually be flagged.


def test_mutation_a_synthetic_unregistered_verb_is_detected(tmp_path: Path) -> None:
    """A verb absent from EVERY VERB_TABLE this module will ever resolve to
    (not just the previous-released tag's) -- the simplest possible bad
    manifest."""
    tag = _previous_released_tag()
    verb_table = _verb_table_at_tag(tag)
    made_up_verb = "totally-made-up-verb-nexus-t9klx-followup"
    assert made_up_verb not in verb_table, "fixture collided with a real verb -- pick another name"

    synthetic = {
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": "nx-hook", "args": [made_up_verb]}]}
            ]
        }
    }
    perturbed = tmp_path / "hooks.json"
    perturbed.write_text(json.dumps(synthetic))

    invocations = _nx_hook_verbs_in_hooks_json(perturbed)
    assert invocations == [(made_up_verb, f"nx-hook {made_up_verb}")], (
        f"extractor found {invocations!r} in the synthetic manifest, expected "
        f"exactly one invocation of {made_up_verb!r} -- the extraction itself "
        "is broken, before offender-detection even runs"
    )
    offenders = [(verb, line) for verb, line in invocations if verb not in verb_table]
    assert offenders, (
        "the synthetic unregistered verb was NOT flagged as an offender -- "
        "the resolution/comparison logic is broken, not merely the extractor"
    )


def test_mutation_a_verb_a_fail_closed_release_lacks_is_detected(tmp_path: Path) -> None:
    """The nexus-t9klx defect shape, pinned so it cannot expire.

    The control this replaced used a verb newer than the PREVIOUS release,
    and went red by design the moment 7.58.0 shipped and registered it (its
    own message said so). The fail-closed release set, 7.55.0 through
    7.57.x, never changes, so a verb one of those releases lacks stays a
    valid control forever. ``auto-approve`` is registered on HEAD and absent
    from v7.55.0 (measured against the published wheel: exit 2).
    """
    from nexus._hook_runtime.entry import VERB_TABLE as head_verb_table  # noqa: PLC0415

    verb = "auto-approve"
    assert verb in head_verb_table, f"{verb!r} is no longer registered on HEAD; pick another"
    known = _verbs_every_fail_closed_release_knows()
    assert verb not in known

    synthetic = {
        "hooks": {
            "PreToolUse": [
                {"hooks": [{"type": "command", "command": "nx-hook", "args": [verb]}]}
            ]
        }
    }
    perturbed = tmp_path / "hooks.json"
    perturbed.write_text(json.dumps(synthetic))
    offenders = [v for v, _ in _nx_hook_verbs_in_hooks_json(perturbed) if v not in known]
    assert offenders == [verb], (
        "a direct nx-hook verb that v7.55.0 lacks was not flagged -- exactly the "
        "nexus-t9klx defect shape this module exists to catch"
    )


# ── The guard itself ─────────────────────────────────────────────────────────


def test_hooks_json_nx_hook_verbs_resolve_in_the_previous_released_wheel() -> None:
    """Every ``nx-hook <verb>`` in ``conexus/hooks/hooks.json`` must be a key
    of the PREVIOUS RELEASED wheel's ``VERB_TABLE`` -- not merely this dev
    tree's, which is always in lockstep with itself by construction and so
    can never see the plugin-ships-before-CLI defect this guards against.

    A hit here means one of:
      1. A verb was wired into ``hooks.json`` before the ``nx`` CLI that
         registers it has shipped in a release. Either wait for that
         release to cut (and the plugin channel to catch up to it), or
         make ``hooks.json`` point at the OLD, already-released form of
         that hook until the CLI ships (nexus-t9klx's bridge: keep the
         plugin-resident ``python3 ${CLAUDE_PLUGIN_ROOT}/...`` exec form
         wired until the CLI that registers the ``nx-hook`` verb is itself
         released).
      2. The previous-released-tag resolution picked the wrong tag --
         check ``test_previous_released_tag_resolves_to_a_real_tag``'s own
         result first.
    """
    tag = _previous_released_tag()
    verb_table = _verb_table_at_tag(tag)
    invocations = _nx_hook_verbs_in_hooks_json()
    offenders = [(verb, line) for verb, line in invocations if verb not in verb_table]
    assert not offenders, (
        f"conexus/hooks/hooks.json names nx-hook verb(s) NOT registered in "
        f"{tag}'s VERB_TABLE (the previous released, currently-installed-"
        f"everywhere nx CLI):\n"
        + "\n".join(f"  {verb!r}: {line}" for verb, line in offenders)
        + f"\n\nA session on the {tag} CLI with this plugin pinned would see "
        "nx-hook SILENTLY NO-OP on every one of these hook events (exit 0 -- or "
        "exit 70, a diagnosable non-zero, for a ledger-shaped verb whose name "
        "starts with 'expectations_') until it self-upgrades via the "
        "version-lockstep hook, which cannot happen if the lockstep hook itself "
        "is one of the offenders. For a DECIDING gate among them "
        "(pre-close-verification, phase-review-close-gate), 'silently no-op' "
        "means the gate DOES NOT RUN AT ALL and the action it would have gated "
        "is ALLOWED -- not merely delayed or degraded (nexus-t9klx, the 7.58.0 "
        "release blocker this test exists to catch before the NEXT one repeats "
        "it)."
    )


# ── Every fail-closed release, not only the previous one (nexus-rcoze) ──────
#
# "Previous released" protects a user one release behind. It does not protect
# a user who skips releases: after 7.58.0 ships, the previous release is
# 7.58.0, whose VERB_TABLE knows every verb above, while a user still on a
# 7.57.0 CLI gets exit 2 from each of them. `nx-hook` fails CLOSED on an
# unknown verb in exactly 7.55.0 (its first release) through 7.57.x, and fails
# open from 7.58.0. That set can never grow, so the rule is permanent: a direct
# `nx-hook` entry must name a verb EVERY one of those releases registers.
# Anything else goes through conexus/hooks/scripts/nx_hook_shim.py, which this
# extractor does not see, because its command is python3. So this rule covers
# DIRECT entries only; shim-routed verbs are checked elsewhere: registered on
# HEAD by tests/test_hooks_json_shape_lint.py, and resolvable by
# tests/hooks/test_hooks_json_handlers_resolve.py (tests/_hook_wiring.command_verb).

_FIRST_NX_HOOK_RELEASE: Version = (7, 55, 0)
_FIRST_FAIL_OPEN_RELEASE: Version = (7, 58, 0)


def _fail_closed_release_tags() -> list[str]:
    proc = _run_git(["tag", "--list", "v*"])
    if proc.returncode != 0:
        pytest.fail(f"git tag --list failed (rc {proc.returncode}): {proc.stderr.strip()}")
    tags: list[tuple[Version, str]] = []
    for line in proc.stdout.splitlines():
        m = _TAG_RE.match(line.strip())
        if not m:
            continue
        version = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if _FIRST_NX_HOOK_RELEASE <= version < _FIRST_FAIL_OPEN_RELEASE:
            tags.append((version, line.strip()))
    if not tags:
        pytest.fail(
            "no v7.55.0 <= tag < v7.58.0 is visible. Fetch tags "
            "(`git fetch --tags`) before trusting this check: a tagless checkout "
            "cannot see the answer, which is not the answer being clean."
        )
    return [t for _, t in sorted(tags)]


def _verbs_every_fail_closed_release_knows() -> set[str]:
    tables = [_verb_table_at_tag(t) for t in _fail_closed_release_tags()]
    return set.intersection(*tables)


def test_the_fail_closed_release_set_is_the_known_one() -> None:
    tags = _fail_closed_release_tags()
    assert tags[0] == "v7.55.0", f"first fail-closed release resolved to {tags[0]}, not v7.55.0"
    assert "v7.57.0" in tags, f"v7.57.0 missing from the fail-closed set: {tags}"
    assert len(tags) >= 6, f"expected at least v7.55.0-.3, v7.56.0, v7.57.0; got {tags}"


def test_positive_control_a_verb_7_55_0_lacks_is_detected() -> None:
    # auto-approve was added after 7.55.0; wired directly, a 7.55.0 CLI exits 2
    # on every conexus MCP tool call (measured against the published wheel).
    assert "auto-approve" not in _verbs_every_fail_closed_release_knows()


def test_direct_nx_hook_entries_resolve_in_every_fail_closed_release() -> None:
    known = _verbs_every_fail_closed_release_knows()
    offenders = sorted({line for verb, line in _nx_hook_verbs_in_hooks_json() if verb not in known})
    assert not offenders, (
        "conexus/hooks/hooks.json runs these nx-hook verbs directly, and at least "
        "one release from 7.55.0 to 7.57.x does not register them, so a user on "
        "that CLI whose plugin updates first gets exit 2 (blocking on "
        "UserPromptSubmit, PreToolUse and PermissionRequest). Route them through "
        "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/scripts/nx_hook_shim.py <verb>: "
        f"{offenders}"
    )
