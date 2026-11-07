# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Plugin changes that are not live yet must be DECLARED, not assumed active.

THE INCIDENT (2026-07-25). A subagent ran ``git stash -u`` in a shared working
tree. The routing guard for that is explicit about ``stash`` and treats the bare
form as destructive -- and it did not fire. Cause: ``marketplace.json`` pins
``plugins[].source.ref`` to an immutable release tag, so Claude Code loads hooks,
commands, skills, and agents from THAT TAG, never from the working tree. The
stash coverage had merged hours earlier. The installed plugin was still v6.18.1.

Three guards had been merged, closed as "mechanized", and were protecting
nothing. A guard BELIEVED live but actually inert is worse than a known-absent
one, because nobody compensates for it.

WHY A LEDGER AND NOT A PLAIN DRIFT FAILURE. The obvious tripwire -- fail when
the surface differs from the pinned tag -- would be RED CONTINUOUSLY between
releases. This repo has documented, more than once, what happens to a check that
cries wolf: it gets blessed reflexively and stops meaning anything. So drift is
not itself a failure. UNDECLARED drift is. Declaring costs one line; the ledger
then doubles as the "what goes live next release" list, which is information
nobody had before.

THE SYMMETRY IS LOAD-BEARING. A stale entry fails too. Once a release ships and
the pin advances, drift returns to zero and the ledger must be emptied -- so it
cannot quietly decay into fiction that everything is pending forever.

WHY THE SURFACE IS BROADER THAN HOOKS. commands/ and skills/ are pinned by the
same mechanism and are equally inert when edited. The same 2026-07-25 session
edited continuation.md and orchestration/SKILL.md believing the corrections took
effect immediately; they did not.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).parent.parent
LEDGER = REPO_ROOT / "conexus" / "PENDING_RELEASE.md"
MARKETPLACE = REPO_ROOT / ".claude-plugin" / "marketplace.json"

#: Per-plugin BEHAVIOURAL surface -- what a session actually executes or reads
#: from the INSTALLED (pinned) copy. Keyed by the plugin name in
#: marketplace.json; ``test_every_marketplace_plugin_has_a_surface`` fails if a
#: plugin ships without an entry here, so a third plugin cannot arrive
#: unnoticed.
#:
#: WHY EACH CONEXUS ENTRY IS IN. hooks/commands/skills/agents are loaded or read
#: by Claude Code from $CLAUDE_PLUGIN_ROOT. resources/ is here because it is
#: dereferenced AT RUNTIME by files that are themselves in the surface --
#: conexus/skills/rdr-create/SKILL.md and conexus/skills/rdr-close/SKILL.md both
#: copy templates from `$CLAUDE_PLUGIN_ROOT/resources/rdr/`, so a template edit
#: is exactly as inert as a hook edit. (Review finding, 2026-07-25: it was
#: originally omitted with no stated reason. The rdr-create COMMAND this
#: comment originally named was itself deleted at nexus-cnzei.4 -- rdr-create
#: is skill-only now -- so the example was updated to two files that still
#: exist.)
#:
#: launch/ holds the script every server and hook entry starts through
#: (RDR-228, nexus-sv2wx.5): it is run from $CLAUDE_PLUGIN_ROOT exactly as a
#: hook script is.
#:
#: WHY THE REST OF conexus/ IS OUT, stated rather than left to luck:
#:   CHANGELOG.md / README.md  docs; stale docs in a shipped plugin are
#:                             harmless, a stale hook is a guard not guarding.
#:   plans/ daemon/            consumed by the separately-versioned PyPI
#:                             package (src/nexus/plans/loader.py), not by the
#:                             plugin loader.
#:   registry.yaml             top-level; only markdown cross-links reference
#:                             it. (Distinct from the nested
#:                             hooks/scripts/routing/registry.yaml, which IS
#:                             covered via the hooks/ prefix.)
#:   retrieval-agents.txt      no live reference in the tree.
#:
#: sn ships its own hooks (subagent injection, MCP auto-approval) and is
#: pinned independently, so it is exposed to the identical inertness class.
#: Each plugin's .mcp.json is on the surface too (nexus-jbt5x): the MCP
#: server spec is loaded from the pinned tag exactly like a hook, and a
#: server pin that changes on develop is inert until the ref advances.
#:
#: conexus-tool-spill (installed from tool-spill/) is an opt-in mod: its hooks
#: module is the behaviour, and its plugin.json is on the surface because the
#: userConfig defaults (the limit and head sizes) live there and the module
#: reads them at load. Its README and tests are not behaviour.
SURFACE_BY_PLUGIN: dict[str, tuple[str, ...]] = {
    "conexus": (
        "conexus/hooks/",
        "conexus/commands/",
        "conexus/skills/",
        "conexus/agents/",
        "conexus/resources/",
        "conexus/mcp/",
        "conexus/launch/",
        "conexus/.mcp.json",
    ),
    "sn": (
        "sn/hooks/",
        "sn/.mcp.json",
    ),
    "conexus-tool-spill": (
        "tool-spill/hooks/",
        "tool-spill/.claude-plugin/plugin.json",
    ),
    "conexus-prose-edit": (
        "prose-edit/skills/",
        "prose-edit/agents/",
        "prose-edit/.claude-plugin/plugin.json",
    ),
}

#: Flattened, for the checks that legitimately do not care which plugin owns a
#: path: the ledger parser (_declared_paths — a declaration is a declaration
#: whichever plugin it names), the surface-exists non-vacuity check, and the
#: workflow path-filter coverage check. DRIFT is never computed over this —
#: each plugin diffs against its OWN ref over its OWN prefixes
#: (_drifted_paths, nexus-a2wmi.14), because after a single-plugin cut the
#: other plugin's older ref would otherwise report the cut's files as its
#: own undeclared drift.
SURFACE: tuple[str, ...] = tuple(
    prefix for prefixes in SURFACE_BY_PLUGIN.values() for prefix in prefixes
)

#: A declared ledger entry: a bullet whose backtick spans contain the path.
#: ONE canonical parser feeds BOTH directions of the contract. The first cut
#: used a raw substring test for "is it declared" and a different bullet parse
#: for "is it stale" -- two mechanisms answering the same question, which let an
#: entry be leniently declared while never being strictly checked for staleness.
_BULLET_SPAN = re.compile(r"^\s*-\s+(.*)$")


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
    )


def _pinned_refs() -> dict[str, str]:
    """Every plugin's pinned release ref, keyed by plugin name.

    The first cut read only the plugin named "conexus", which silently exempted
    sn -- a second, independently-pinned plugin that ships its own routing guard
    and is exposed to the identical inertness class (review finding).
    """
    data = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
    refs: dict[str, str] = {}
    for plugin in data.get("plugins", []):
        name, source = plugin.get("name"), plugin.get("source")
        if name and isinstance(source, dict) and source.get("ref"):
            refs[str(name)] = str(source["ref"])
    assert refs, (
        "marketplace.json declares no plugin with a source.ref -- the pinned "
        "release model this test depends on has changed shape. Do not delete "
        "this test; update it to the new shape."
    )
    return refs


def _declared_paths() -> set[str]:
    """Exact set of repo paths declared in the ledger.

    EXACT, not substring. `p in ledger_text` reported an UNDECLARED file as
    declared whenever its path was a textual prefix of a declared one -- e.g.
    `conexus/hooks/scripts/expectations` (no extension) rides on the declared
    `conexus/hooks/scripts/expectations.sh`. Reproduced during review.

    Reads EVERY backtick span on a bullet, not just the first, so an entry that
    leads with a non-path span (`` - `git stash -u` is covered by `path` ``)
    still has its path captured. The first cut took span[1] only, which meant
    such an entry escaped staleness detection permanently.
    """
    declared: set[str] = set()
    for line in _ledger_text().splitlines():
        m = _BULLET_SPAN.match(line)
        if not m:
            continue
        for span in re.findall(r"`([^`]+)`", m.group(1)):
            span = span.strip()
            if any(span.startswith(prefix) for prefix in SURFACE):
                declared.add(span)
    return declared


def _has_any_tags() -> bool:
    return bool(_git("tag", "-l").stdout.strip())


def _drifted_paths(plugin: str, ref: str) -> list[str]:
    """Paths under *plugin*'s OWN surface that differ from *its* ref.

    Scoped per plugin (nexus-a2wmi.14): after a conexus-only cut, sn's
    ref still names the older client tag, and diffing it over the
    flattened surface would return the conexus files the cut just
    shipped — undeclared drift caused by the channel working correctly.
    """
    proc = _git("diff", "--name-only", f"{ref}..HEAD", "--", *SURFACE_BY_PLUGIN[plugin])
    # A FAILED diff must never read as "no drift". That silent-zero is the whole
    # bug class this file exists for.
    assert proc.returncode == 0, (
        f"git diff against {ref} failed, so drift is UNKNOWN, not zero:\n"
        f"{proc.stderr}"
    )
    return sorted(p for p in proc.stdout.splitlines() if p.strip())


def _ledger_text() -> str:
    return LEDGER.read_text(encoding="utf-8") if LEDGER.exists() else ""


# --- taglessness: skip locally, but NEVER silently in CI ---------------------
#
# The drift checks need the pinned tag resolvable. On a developer's shallow
# clone it is not, and skipping is right. In CI it MUST NOT be: if the job's
# tag-fetch step ever breaks, a skip would report green having checked nothing
# -- the identical believed-live-but-inert bug, one level up, inside the very
# thing built to detect it. So the dedicated CI job sets this flag and
# taglessness becomes a hard failure there.
_REQUIRE_ENV = "NX_REQUIRE_PLUGIN_DRIFT_CHECK"


def _pyproject_version() -> str:
    import tomllib

    with open(REPO_ROOT / "pyproject.toml", "rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def _remote_confirms_tag_absent(ref: str) -> bool | None:
    """Authoritative upstream probe (nexus-icrwy): does origin have *ref*?

    Local unresolvability cannot distinguish "tag genuinely not cut yet"
    (the release window) from "tag exists upstream but this checkout's
    fetch failed" — and ref == v<pyproject> is develop's CONTINUOUS
    resting state between releases, not a seconds-long branch condition.
    Returns True (confirmed absent upstream → genuine window), False
    (present upstream → the fetch step is broken, NOT a window), or None
    (probe itself failed — offline/auth — cannot confirm either way).
    """
    proc = _git("ls-remote", "--tags", "origin", f"refs/tags/{ref}")
    if proc.returncode != 0:
        return None
    return not proc.stdout.strip()


def _require_or_skip() -> None:
    if _has_any_tags():
        return
    if _in_release_window():
        # RELEASE-WINDOW-SHAPED (ref == v<pyproject>, tag locally
        # unresolvable): the pinned ref cannot be fetched when it does not
        # exist yet — the workflow's fetch step tolerates exactly this
        # case, so a tagless checkout here CAN be the documented
        # sequencing (exposed at the 7.4.0 cut: nexus-05m1i made this job
        # honest, and the first honest release-window run hit the
        # require-flag raise below, which every prior cut had sailed past
        # only because the job was vacuous-green). But window-SHAPED is
        # not window-CONFIRMED (nexus-icrwy, critic Critical on the first
        # fix): local unresolvability also matches "fetch step broke",
        # and ref == v<pyproject> is develop's resting state between
        # releases. Confirm absence UPSTREAM before trusting the window;
        # an inconclusive probe fails closed under the require flag.
        # Probe the refs ACTUALLY PINNED, per plugin — never a
        # reconstructed "v" + pyproject string, which checks the wrong
        # ref the moment a plugin pins an older client tag.
        confirmations = {
            _remote_confirms_tag_absent(ref)
            for ref in set(_pinned_refs().values())
        }
        confirmed: bool | None
        if confirmations == {True}:
            confirmed = True
        elif False in confirmations:
            confirmed = False
        else:
            confirmed = None
        if confirmed is True:
            # Genuine window: the ledger-EMPTY contract the tests then
            # enforce needs no tag to check.
            return
        if os.environ.get(_REQUIRE_ENV) == "1":
            raise AssertionError(
                f"{_REQUIRE_ENV}=1, checkout is tagless and release-window-"
                f"shaped, but upstream "
                + (
                    "HAS the pinned tag — the tag-fetch step is broken, this "
                    "is NOT a release window"
                    if confirmed is False
                    else "absence could not be confirmed (ls-remote failed) — "
                    "refusing to treat an unverifiable state as a window"
                )
                + ". Failing loudly rather than checking nothing (nexus-icrwy)."
            )
        pytest.skip(
            "tagless and release-window-shaped, but upstream absence not "
            "confirmed — drift unresolvable locally."
        )
    if os.environ.get(_REQUIRE_ENV) == "1":
        raise AssertionError(
            f"{_REQUIRE_ENV}=1 but this checkout has NO tags, so the pinned ref "
            "cannot be resolved and drift is UNKNOWN. In CI this means the "
            "tag-fetch step did not do its job. Failing loudly rather than "
            "skipping, because a skip here reports green while checking nothing."
        )
    pytest.skip(
        "no tags in this checkout (shallow clone) -- drift unresolvable. "
        f"Verified genuinely tagless, not assumed. CI sets {_REQUIRE_ENV}=1 so "
        "this path cannot go unnoticed there."
    )


# --- non-vacuity: this file must not pass by checking nothing ----------------


def test_the_surface_prefixes_all_exist() -> None:
    """A renamed or deleted directory would silently shrink coverage to zero."""
    missing = [
        p for p in SURFACE
        if not ((REPO_ROOT / p).is_dir() if p.endswith("/") else (REPO_ROOT / p).is_file())
    ]
    assert not missing, (
        f"SURFACE names paths that do not exist: {missing}. Either the "
        "plugin layout moved (update SURFACE) or a path was deleted. Until "
        "corrected, the drift check is watching nothing."
    )


def test_the_ledger_file_exists() -> None:
    assert LEDGER.exists(), (
        f"{LEDGER.relative_to(REPO_ROOT)} is missing. It is the acknowledgement "
        "ledger for plugin changes that are not live yet; deleting it does not "
        "make the gap go away, it makes it invisible again."
    )


def test_every_marketplace_plugin_has_a_surface() -> None:
    """A third plugin must not arrive unnoticed.

    sn was originally outside this file entirely, despite shipping its own
    routing guard and being pinned independently -- the identical inertness
    class, silently exempt (review finding, 2026-07-25).
    """
    unmapped = sorted(set(_pinned_refs()) - set(SURFACE_BY_PLUGIN))
    assert not unmapped, (
        f"marketplace.json ships plugin(s) with no SURFACE entry: {unmapped}. "
        "Their behavioural files are pinned exactly like conexus's, so they are "
        "equally inert when edited -- and currently unchecked. Add a surface."
    )


# --- the pin itself ----------------------------------------------------------


def plugin_in_release_window(plugin: str, ref: str) -> bool:
    """Per-plugin release window, judged for ONE plugin's pinned ref alone.

    The window is "the tag this branch is about to cut does not exist yet":
    the ref is ``v<pyproject version>`` and does not resolve. A ref that
    resolves is never a window (nothing is pending), and any other
    unresolvable ref is not one either, so a missing tag stays RED. The
    anchored ``plugin-vX.Y.Z-n`` form is retired (nexus-rbmja.8, .9) and
    reads as not-a-window like any other shape.

    Its known dangling-tag hole is accepted: a cut that merges and whose
    tag is never pushed keeps the ledger-EMPTY contract in force until the
    next client release.
    """
    if _git("rev-parse", "--verify", f"{ref}^{{commit}}").returncode == 0:
        return False  # the tag exists: nothing is pending, no window
    return ref == f"v{_pyproject_version()}"


def _in_release_window() -> bool:
    """Whole-checkout composite, ONLY for _require_or_skip's tagless path.

    The tagless branch asks "is this tagless checkout explainable as a
    release window" -- a checkout-level question, so it composes the
    per-plugin predicate over every pin.
    """
    return all(
        plugin_in_release_window(name, ref)
        for name, ref in _pinned_refs().items()
    )


def test_every_pinned_ref_is_a_real_tag() -> None:
    _require_or_skip()
    for name, ref in sorted(_pinned_refs().items()):
        if plugin_in_release_window(name, ref):
            continue  # ref names the tag this very branch cuts; created at merge
        proc = _git("rev-parse", "--verify", f"{ref}^{{commit}}")
        assert proc.returncode == 0, (
            f"marketplace.json pins {name} at source.ref={ref!r}, but that tag "
            f"does not exist. Installed users resolve the plugin from that ref, "
            f"so it must be a real, pushed tag."
        )


# --- the ledger contract, both directions off ONE parsed set ----------------


def _declared_paths_for(plugin: str) -> set[str]:
    """The ledger's declarations under *plugin*'s own surface prefixes."""
    return {
        path
        for path in _declared_paths()
        if any(path.startswith(prefix) for prefix in SURFACE_BY_PLUGIN[plugin])
    }


def _deferred_declared_paths() -> set[str]:
    """DEFERRED entries' channel paths (nexus-2x3qy) -- exempt from the
    release-window "ledger must be empty" contract below.

    A deferred entry says its straddling bead is intentionally held back
    from THIS release too (the cherry-pick skips it),
    so it stays declared even while the plugin's pin advances to the tag
    this branch cuts -- still declared is correct there, not stale.
    Reuses `plugin_ledger.deferred_paths`, the one parser of the
    "## Deferred to the next client release" section, rather than a
    second copy that could drift from what the release tooling itself reads.
    """
    from plugin_ledger import deferred_paths

    return deferred_paths(_ledger_text())


def test_every_drifted_file_is_declared_in_the_ledger() -> None:
    """Undeclared drift is the failure -- drift itself is not.

    Judged per plugin (nexus-a2wmi.4): a plugin in its release window has
    zero drift by construction — its ref IS the content this branch cuts
    — so its contract reduces to "its ledger entries are empty" (the pin
    advance ships them). Every other plugin is checked strictly against
    its own ref over its own surface (.14's scoping). DEFERRED entries
    are the one exception (nexus-2x3qy): the pin advance does NOT ship
    them (the cut holds their paths back), so they stay declared.
    """
    _require_or_skip()
    drifted: set[str] = set()
    deferred = _deferred_declared_paths()
    for plugin, ref in _pinned_refs().items():
        if plugin_in_release_window(plugin, ref):
            assert not (_declared_paths_for(plugin) - deferred), (
                f"{plugin} is in its release window (ref {ref!r} is the tag "
                f"this branch cuts) but the PENDING_RELEASE ledger still "
                f"lists its files — the pin advance ships them, so its "
                f"entries must be emptied in the cut commit (or moved under "
                f"'## Deferred to the next client release' if they were "
                f"deliberately held back)."
            )
            continue
        drifted |= set(_drifted_paths(plugin, ref))
    if not drifted:
        return
    declared = _declared_paths()

    undeclared = sorted(drifted - declared)
    assert not undeclared, (
        "These plugin files differ from the pinned release tag but are NOT "
        "declared in conexus/PENDING_RELEASE.md:\n"
        + "\n".join(f"  {p}" for p in undeclared)
        + "\n\nThey are INERT: Claude Code loads the plugin from the pinned "
        "tag, so these changes do not run in ANY session until the next release "
        "ships.\nFIX: add each path to conexus/PENDING_RELEASE.md with one line "
        "saying what it changes and which bead. Do NOT weaken this test instead "
        "-- the entry is the honest statement that the change is not yet live."
    )


def test_the_ledger_has_no_stale_entries() -> None:
    """After a release the pin advances, drift goes to zero, and this empties.

    Uses the SAME parsed set as the check above. The first cut used a lenient
    substring test one way and a stricter bullet parse the other, so an entry
    could count as declared while never being checked for staleness.
    """
    _require_or_skip()
    declared: set[str] = set()
    drifted: set[str] = set()
    deferred = _deferred_declared_paths()
    for plugin, ref in _pinned_refs().items():
        if plugin_in_release_window(plugin, ref):
            assert not (_declared_paths_for(plugin) - deferred), (
                f"{plugin} is in its release window: its ledger entries must "
                f"be EMPTY, deferred entries aside (see "
                f"test_every_drifted_file_is_declared_in_the_ledger)."
            )
            continue
        declared |= _declared_paths_for(plugin)
        drifted |= set(_drifted_paths(plugin, ref))

    stale = sorted(declared - drifted)
    assert not stale, (
        "conexus/PENDING_RELEASE.md lists files that NO LONGER differ from the "
        "pinned tag -- they have shipped:\n"
        + "\n".join(f"  {p}" for p in stale)
        + "\n\nRemove them. A ledger of already-shipped changes is "
        "indistinguishable from a ledger of pending ones."
    )


# --- the skip/require mechanism is itself tested ----------------------------


class TestTaglessBehaviour:
    """An untested safety mechanism is not a safety mechanism."""

    def test_tagless_without_the_flag_skips(self, monkeypatch) -> None:
        monkeypatch.delenv(_REQUIRE_ENV, raising=False)
        monkeypatch.setattr(
            "tests.test_plugin_release_drift_ledger._has_any_tags", lambda: False
        )
        # Pin the window OFF: on an actual release branch the real checkout
        # IS in the window, and this test is about NON-window taglessness.
        monkeypatch.setattr(
            "tests.test_plugin_release_drift_ledger._in_release_window",
            lambda: False,
        )
        with pytest.raises(BaseException) as exc:
            _require_or_skip()
        assert exc.typename == "Skipped", f"expected a skip, got {exc.typename}"

    def test_tagless_with_the_flag_fails_loudly(self, monkeypatch) -> None:
        monkeypatch.setenv(_REQUIRE_ENV, "1")
        monkeypatch.setattr(
            "tests.test_plugin_release_drift_ledger._has_any_tags", lambda: False
        )
        monkeypatch.setattr(
            "tests.test_plugin_release_drift_ledger._in_release_window",
            lambda: False,
        )
        with pytest.raises(AssertionError, match="tag-fetch step"):
            _require_or_skip()

    def test_tagless_in_confirmed_release_window_proceeds_even_with_flag(
        self, monkeypatch
    ) -> None:
        """The 7.4.0-cut regression: in the release window the pinned tag
        cannot exist yet, so taglessness is the documented sequencing —
        the require flag must NOT turn it into a failure (and no skip:
        the window contract, ledger-EMPTY, is checked tag-free). The
        window must be CONFIRMED upstream (nexus-icrwy)."""
        monkeypatch.setenv(_REQUIRE_ENV, "1")
        monkeypatch.setattr(
            "tests.test_plugin_release_drift_ledger._has_any_tags", lambda: False
        )
        monkeypatch.setattr(
            "tests.test_plugin_release_drift_ledger._in_release_window",
            lambda: True,
        )
        monkeypatch.setattr(
            "tests.test_plugin_release_drift_ledger._remote_confirms_tag_absent",
            lambda ref: True,
        )
        _require_or_skip()  # returns, no raise, no skip

    def test_window_shaped_but_tag_exists_upstream_fails_loudly(
        self, monkeypatch
    ) -> None:
        """nexus-icrwy (critic Critical): ref == v<pyproject> is develop's
        RESTING state between releases, so window-shaped + tagless can
        also mean 'the fetch step broke'. Upstream having the tag proves
        exactly that — never a window, always a loud failure."""
        monkeypatch.setenv(_REQUIRE_ENV, "1")
        monkeypatch.setattr(
            "tests.test_plugin_release_drift_ledger._has_any_tags", lambda: False
        )
        monkeypatch.setattr(
            "tests.test_plugin_release_drift_ledger._in_release_window",
            lambda: True,
        )
        monkeypatch.setattr(
            "tests.test_plugin_release_drift_ledger._remote_confirms_tag_absent",
            lambda ref: False,
        )
        with pytest.raises(AssertionError, match="NOT a release window"):
            _require_or_skip()

    def test_window_shaped_but_probe_inconclusive_fails_closed_with_flag(
        self, monkeypatch
    ) -> None:
        """An unverifiable window is not a window: ls-remote failing
        (offline/auth) under the require flag raises — the CI runner just
        cloned from origin, so no-network THERE is itself suspicious."""
        monkeypatch.setenv(_REQUIRE_ENV, "1")
        monkeypatch.setattr(
            "tests.test_plugin_release_drift_ledger._has_any_tags", lambda: False
        )
        monkeypatch.setattr(
            "tests.test_plugin_release_drift_ledger._in_release_window",
            lambda: True,
        )
        monkeypatch.setattr(
            "tests.test_plugin_release_drift_ledger._remote_confirms_tag_absent",
            lambda ref: None,
        )
        with pytest.raises(AssertionError, match="could not be confirmed"):
            _require_or_skip()

    def test_window_shaped_probe_inconclusive_without_flag_skips(
        self, monkeypatch
    ) -> None:
        """Locally (no require flag), an unconfirmable window degrades to
        a skip — offline dev boxes must not hard-fail."""
        monkeypatch.delenv(_REQUIRE_ENV, raising=False)
        monkeypatch.setattr(
            "tests.test_plugin_release_drift_ledger._has_any_tags", lambda: False
        )
        monkeypatch.setattr(
            "tests.test_plugin_release_drift_ledger._in_release_window",
            lambda: True,
        )
        monkeypatch.setattr(
            "tests.test_plugin_release_drift_ledger._remote_confirms_tag_absent",
            lambda ref: None,
        )
        with pytest.raises(BaseException) as exc:
            _require_or_skip()
        assert exc.typename == "Skipped", f"expected a skip, got {exc.typename}"


class TestTheCIWiringItself:
    """The guard guards its own wiring.

    Everything above is worthless if the workflow that runs it loses the flag,
    stops fetching tags, or stops targeting this file. Each of those reverts it
    to skipping silently in CI -- the original defect. These are cheap YAML
    assertions and they close the loop.
    """

    @staticmethod
    def _workflow() -> dict:
        yaml = pytest.importorskip("yaml")
        path = REPO_ROOT / ".github" / "workflows" / "plugin-drift-ledger.yml"
        assert path.exists(), (
            "the dedicated drift-ledger workflow is gone. Without it these "
            "tests only ever run on a developer's machine, which is exactly "
            "how this guard was found to be inert in the first place."
        )
        return yaml.safe_load(path.read_text(encoding="utf-8"))

    def test_the_workflow_sets_the_require_flag(self) -> None:
        steps = self._workflow()["jobs"]["ledger"]["steps"]
        envs = [s.get("env", {}) for s in steps]
        assert any(e.get(_REQUIRE_ENV) == "1" for e in envs), (
            f"the workflow no longer sets {_REQUIRE_ENV}=1, so a broken "
            "tag-fetch would make these tests SKIP and the job report green "
            "having checked nothing."
        )

    def test_the_workflow_actually_fetches_tags(self) -> None:
        steps = self._workflow()["jobs"]["ledger"]["steps"]
        runs = " ".join(s.get("run", "") for s in steps)
        assert "refs/tags/" in runs and "git fetch" in runs, (
            "the workflow no longer fetches the pinned tags; every drift test "
            "would fail on the require-flag instead of actually checking drift"
        )

    def test_the_workflow_runs_this_file(self) -> None:
        steps = self._workflow()["jobs"]["ledger"]["steps"]
        runs = " ".join(s.get("run", "") for s in steps)
        assert "test_plugin_release_drift_ledger.py" in runs, (
            "the workflow no longer targets this test file"
        )

    def test_the_path_filter_covers_every_surface_prefix(self) -> None:
        """A surface prefix outside the trigger paths is a surface whose drift
        never triggers the check -- undeclared drift merging on a green PR."""
        wf = self._workflow()
        on = wf[True] if True in wf else wf["on"]
        patterns = " ".join(on["push"]["paths"])
        missing = [p for p in SURFACE if p.rstrip("/") not in patterns]
        assert not missing, (
            f"SURFACE prefixes not covered by the workflow's path filter: "
            f"{missing}. Drift there would never trigger this workflow, so it "
            f"could merge undeclared on a green PR."
        )

    def test_the_path_filter_covers_the_ledger_module_the_test_imports(self) -> None:
        """nexus-rbmja.5: the ledger parser moved to ``scripts/plugin_ledger.py``
        (rbmja.2); an edit there changes what this check computes, so it must
        trigger the workflow as the cut script's own path does."""
        wf = self._workflow()
        on = wf[True] if True in wf else wf["on"]
        for event in ("push", "pull_request"):
            assert "scripts/plugin_ledger.py" in on[event]["paths"], event

    def _fetch_step(self) -> dict:
        steps = self._workflow()["jobs"]["ledger"]["steps"]
        matches = [
            s for s in steps if "Fetch exactly the pinned" in s.get("name", "")
        ]
        assert len(matches) == 1, "the tag-fetch step is gone or renamed"
        return matches[0]

    def test_the_fetch_step_fetches_all_tags(self) -> None:
        """The tag list must not be empty on this runner: ``_has_any_tags``
        reads it to tell a checkout that fetched tags from a tagless one."""
        assert "git fetch --depth=1 --tags --force origin" in self._fetch_step()["run"], (
            "the explicit shallow --tags fetch is gone: a checkout with no "
            "tags reads as tagless and every drift test fails on the require "
            "flag instead of checking drift."
        )

    def test_the_fetch_step_no_longer_tolerates_the_anchored_shape(self) -> None:
        """The anchored plugin-v<version>-<n> channel is retired
        (nexus-rbmja.8) and nothing produces such a ref, so the per-ref bash
        loop tolerates only v<pyproject>; an absent anchored ref hard-exits
        like any other unresolvable one."""
        run = self._fetch_step()["run"]
        assert "plugin-v" not in run, (
            "the fetch loop still names the retired anchored tag shape and "
            "would wave an absent plugin-vX.Y.Z-N ref through"
        )
        assert 'if [ "$ref" = "v$version" ]; then' in run, (
            "the client release window tolerance is gone from the fetch loop"
        )


class TestPerPluginDriftScoping:
    """RDR-197 (nexus-a2wmi.14): drift is judged per plugin, ref by ref.

    When one plugin's ref moves to a newer client tag and its sibling's
    still points at the older one, diffing the older ref against HEAD over
    the FLATTENED surface returns the files the newer ref just shipped; the
    release emptied the ledger, so those paths read as undeclared drift and
    the contract fails on develop. The fix is scoping each plugin's diff to
    its own surface, and these tests pin it from both directions: the move
    must not fail the other plugin, and scoping must not blind the check to
    that plugin's genuine drift.
    """

    SN_PROBE = "sn/hooks/probe.py"

    @classmethod
    def _cut_world(
        cls,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        sn_drift: bool,
    ) -> pathlib.Path:
        """A repo whose conexus pin is one client tag past sn's.

        History: baseline (tag v9.9.0, both plugins pinned there) → the
        conexus release (a conexus/hooks/ change + conexus's pin advanced
        to v9.9.1, tagged) → later develop (a conexus/commands/ change,
        DECLARED in the ledger; with *sn_drift*, an UNDECLARED sn/hooks/
        change too). The production test functions then run against this
        world via the module globals.
        """
        repo = tmp_path / "cutworld"
        repo.mkdir()

        def run(*args: str) -> None:
            subprocess.run(
                ["git", *args], cwd=repo, check=True, capture_output=True,
                text=True,
            )

        def write(rel: str, content: str) -> None:
            target = repo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        def marketplace(conexus_ref: str) -> str:
            return json.dumps(
                {
                    "plugins": [
                        {
                            "name": "conexus",
                            "source": {"source": "git-subdir", "ref": conexus_ref},
                        },
                        {
                            "name": "sn",
                            "source": {"source": "git-subdir", "ref": "v9.9.0"},
                        },
                    ]
                }
            )

        run("init", "-q", "-b", "main")
        run("config", "user.email", "test@test.invalid")
        run("config", "user.name", "test")
        write("conexus/hooks/hook.py", "v1\n")
        write("conexus/commands/cmd.md", "v1\n")
        write(cls.SN_PROBE, "v1\n")
        write(".claude-plugin/marketplace.json", marketplace("v9.9.0"))
        write("conexus/PENDING_RELEASE.md", "# Pending\n")
        write("pyproject.toml", '[project]\nname = "w"\nversion = "9.9.0"\n')
        run("add", ".")
        run("commit", "-q", "-m", "baseline")
        run("tag", "v9.9.0")

        write("conexus/hooks/hook.py", "v2 shipped by the release\n")
        write(".claude-plugin/marketplace.json", marketplace("v9.9.1"))
        run("add", ".")
        run("commit", "-q", "-m", "conexus release")
        run("tag", "v9.9.1")

        write("conexus/commands/cmd.md", "v2 later work\n")
        write(
            "conexus/PENDING_RELEASE.md",
            "# Pending\n- `conexus/commands/cmd.md`: later change (fixture)\n",
        )
        if sn_drift:
            write(cls.SN_PROBE, "v2 UNDECLARED\n")
        run("add", ".")
        run("commit", "-q", "-m", "later develop")

        module = sys.modules[__name__]
        monkeypatch.setattr(module, "REPO_ROOT", repo)
        monkeypatch.setattr(
            module, "MARKETPLACE", repo / ".claude-plugin" / "marketplace.json"
        )
        monkeypatch.setattr(
            module, "LEDGER", repo / "conexus" / "PENDING_RELEASE.md"
        )
        return repo

    def test_a_newer_conexus_pin_reports_no_failure_for_sn(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A release that moves one plugin's pin must not turn develop red.

        Restore the flattened-surface diff and this fails: sn's older ref
        picks up the release's conexus files as undeclared drift.
        """
        self._cut_world(tmp_path, monkeypatch, sn_drift=False)
        test_every_drifted_file_is_declared_in_the_ledger()
        test_the_ledger_has_no_stale_entries()

    def test_genuine_sn_drift_is_still_caught_while_refs_differ(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Per-plugin scoping must not narrow the check into blindness.

        Stub the per-plugin diff to return nothing and this fails: the
        undeclared sn change would sail through.
        """
        self._cut_world(tmp_path, monkeypatch, sn_drift=True)
        with pytest.raises(AssertionError, match=re.escape(self.SN_PROBE)):
            test_every_drifted_file_is_declared_in_the_ledger()


class TestDeferralExemptsTheReleaseWindow:
    """nexus-2x3qy: a DEFERRED entry stays declared even while its plugin's
    pin advances to the tag THIS branch cuts, because the cut held that
    entry's content back -- the pin advance does not ship it, so "still
    declared" is correct there, not the staleness the window contract
    otherwise demands (test_every_drifted_file_is_declared_in_the_ledger's
    and test_the_ledger_has_no_stale_entries's own window branches, both
    exercised here since they share the exemption).
    """

    @staticmethod
    def _world(
        tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, *, deferred: bool
    ) -> pathlib.Path:
        repo = tmp_path / "windowworld"
        repo.mkdir()

        def run(*args: str) -> None:
            subprocess.run(
                ["git", *args], cwd=repo, check=True, capture_output=True, text=True
            )

        def write(rel: str, content: str) -> None:
            target = repo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        run("init", "-q", "-b", "main")
        run("config", "user.email", "test@test.invalid")
        run("config", "user.name", "test")
        write(
            ".claude-plugin/marketplace.json",
            json.dumps(
                {
                    "plugins": [
                        {
                            "name": "conexus",
                            "source": {"source": "git-subdir", "ref": "v9.9.0"},
                        }
                    ]
                }
            ),
        )
        ledger = "# Pending\n"
        if deferred:
            ledger += (
                "\n## Deferred to the next client release\n\n"
                "- `conexus/hooks/held.py`: held back (nexus-zzzzz)\n"
            )
        else:
            ledger += "- `conexus/hooks/held.py`: not deferred (nexus-zzzzz)\n"
        write("conexus/PENDING_RELEASE.md", ledger)
        write("pyproject.toml", '[project]\nname = "w"\nversion = "9.9.0"\n')
        run("add", ".")
        run("commit", "-q", "-m", "baseline")
        run("tag", "v9.9.0")

        module = sys.modules[__name__]
        monkeypatch.setattr(module, "REPO_ROOT", repo)
        monkeypatch.setattr(
            module, "MARKETPLACE", repo / ".claude-plugin" / "marketplace.json"
        )
        monkeypatch.setattr(module, "LEDGER", repo / "conexus" / "PENDING_RELEASE.md")
        # The window predicate itself is exercised in TestReleaseWindow-
        # Behaviour below; here it is forced True so both tests isolate
        # the exemption, the thing actually under test.
        monkeypatch.setattr(module, "plugin_in_release_window", lambda name, ref: True)
        return repo

    def test_a_deferred_entry_passes_both_window_contracts(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._world(tmp_path, monkeypatch, deferred=True)
        test_every_drifted_file_is_declared_in_the_ledger()
        test_the_ledger_has_no_stale_entries()

    def test_a_non_deferred_entry_still_fails_both_window_contracts(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-vacuity: the SAME entry text, only moved out from under the
        heading, must still be caught -- the exemption is section-scoped,
        not a blanket pass for any entry naming this path."""
        self._world(tmp_path, monkeypatch, deferred=False)
        with pytest.raises(AssertionError, match="release window"):
            test_every_drifted_file_is_declared_in_the_ledger()
        with pytest.raises(AssertionError, match="release window"):
            test_the_ledger_has_no_stale_entries()


# ---------------------------------------------------------------------------
# The release window, per plugin.
# ---------------------------------------------------------------------------


class TestReleaseWindowBehaviour:
    """plugin_in_release_window, one case per test.

    Mirrors TestTaglessBehaviour's monkeypatch style: every collaborator
    is stubbed except the condition under test.
    """

    @pytest.fixture(autouse=True)
    def _stub_world(self, monkeypatch: pytest.MonkeyPatch):
        module = sys.modules[__name__]
        monkeypatch.setattr(module, "_pyproject_version", lambda: "9.9.0")
        # Tag does not resolve locally (the window's precondition).
        monkeypatch.setattr(
            module,
            "_git",
            lambda *args: subprocess.CompletedProcess(args, 1, stdout="", stderr=""),
        )
        self.monkeypatch = monkeypatch
        self.module = module

    def test_the_client_form_with_no_tag_is_a_window(self) -> None:
        assert plugin_in_release_window("sn", "v9.9.0") is True

    def test_the_client_form_with_an_existing_tag_is_no_window(self) -> None:
        self.monkeypatch.setattr(
            self.module,
            "_git",
            lambda *args: subprocess.CompletedProcess(args, 0, stdout="ok", stderr=""),
        )
        assert plugin_in_release_window("sn", "v9.9.0") is False

    def test_a_missing_tag_for_another_version_is_no_window(self) -> None:
        """A ref that is not the tag this branch cuts stays RED, not exempt."""
        assert plugin_in_release_window("conexus", "v9.8.0") is False

    def test_the_retired_anchored_form_is_no_window(self) -> None:
        assert plugin_in_release_window("conexus", "plugin-v9.9.0-1") is False


class TestReleaseWindowComposite:
    """The composite _in_release_window, with only the per-plugin predicate
    stubbed so the composition under test runs."""

    def _pins(self, monkeypatch, predicate) -> None:
        module = sys.modules[__name__]
        monkeypatch.setattr(
            module,
            "_pinned_refs",
            lambda: {"conexus": "v9.9.0", "sn": "v9.8.0"},
        )
        monkeypatch.setattr(module, "plugin_in_release_window", predicate)

    def test_all_windowed_composes_to_true(self, monkeypatch) -> None:
        self._pins(monkeypatch, lambda name, ref: True)
        assert _in_release_window() is True

    def test_one_unwindowed_composes_to_false(self, monkeypatch) -> None:
        self._pins(monkeypatch, lambda name, ref: ref != "v9.8.0")
        assert _in_release_window() is False


def test_the_tagless_probe_targets_the_pinned_refs(monkeypatch) -> None:
    """The upstream probe checks the refs ACTUALLY pinned, never a
    reconstructed "v" + pyproject string, which checks the wrong ref the
    moment a plugin pins an older client tag."""
    module = sys.modules[__name__]
    probed: list[str] = []
    monkeypatch.delenv(_REQUIRE_ENV, raising=False)
    monkeypatch.setattr(module, "_has_any_tags", lambda: False)
    monkeypatch.setattr(module, "_in_release_window", lambda: True)
    monkeypatch.setattr(
        module,
        "_pinned_refs",
        lambda: {"conexus": "v9.9.0", "sn": "v9.8.0"},
    )

    def recording(ref: str) -> bool:
        probed.append(ref)
        return True

    monkeypatch.setattr(module, "_remote_confirms_tag_absent", recording)
    _require_or_skip()  # window confirmed: returns
    assert sorted(probed) == ["v9.8.0", "v9.9.0"]
