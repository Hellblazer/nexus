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
#: conexus/commands/rdr-create.md and conexus/skills/rdr-create/SKILL.md both
#: copy templates from `$CLAUDE_PLUGIN_ROOT/resources/rdr/`, so a template edit
#: is exactly as inert as a hook edit. (Review finding, 2026-07-25: it was
#: originally omitted with no stated reason.)
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
#: sn ships its own routing guard (grep_for_symbols_redirects_to_serena.py) and
#: is pinned independently, so it is exposed to the identical inertness class.
SURFACE_BY_PLUGIN: dict[str, tuple[str, ...]] = {
    "conexus": (
        "conexus/hooks/",
        "conexus/commands/",
        "conexus/skills/",
        "conexus/agents/",
        "conexus/resources/",
    ),
    "sn": (
        "sn/hooks/",
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
        # ref the moment any plugin pins the anchored form (site 3,
        # nexus-a2wmi.4).
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
    missing = [p for p in SURFACE if not (REPO_ROOT / p).is_dir()]
    assert not missing, (
        f"SURFACE names directories that do not exist: {missing}. Either the "
        "plugin layout moved (update SURFACE) or a directory was deleted. Until "
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
    """Per-plugin release window (invariant W — nexus-a2wmi.4, RDR-197).

    Invariant W's four conditions and the reasoning behind them live in
    scripts/plugin_channel.py's module docstring; this predicate
    implements them per plugin, never quantified across refs — on a cut
    PR one plugin is anchored with its tag not yet cut while the other
    still pins the client tag, and a quantifier is satisfied by neither
    branch.

    CLIENT form (``v<pyproject version>``): the window is "tag not yet
    cut" — the pre-existing whole-checkout contract, now judged per
    plugin. Its known dangling-tag hole is accepted (scope note, bead
    .1); the closer applies to the anchored path only.

    ANCHORED form: all four conditions of invariant W — shape valid for
    the CURRENT version (a); n equal to next_plugin_tag_number, which
    establishes tag visibility first and raises TagVisibilityError on a
    blind checkout (b); upstream-confirmed tag absence, fail-closed on
    an inconclusive probe (c); and the checkout being the cut branch
    itself (d). A plugin whose tag resolves is never in a window.
    """
    from plugin_channel import (
        is_cut_branch_for,
        next_plugin_tag_number,
        parse_plugin_tag,
    )

    version = _pyproject_version()
    if _git("rev-parse", "--verify", f"{ref}^{{commit}}").returncode == 0:
        return False  # the tag exists: nothing is pending, no window
    if ref == f"v{version}":
        return True  # client window: this branch cuts that very tag
    parsed = parse_plugin_tag(ref)
    if parsed is None or parsed[0] != version:  # condition (a)
        return False
    _, n = parsed
    if n != next_plugin_tag_number(version, cwd=REPO_ROOT):  # condition (b)
        return False
    if _remote_confirms_tag_absent(ref) is not True:  # condition (c), fail-closed
        return False
    return is_cut_branch_for(version, n, cwd=REPO_ROOT)  # condition (d)


def _in_release_window() -> bool:
    """Whole-checkout composite, ONLY for _require_or_skip's tagless path.

    The tagless branch asks "is this tagless checkout explainable as a
    release window" — a checkout-level question, so it composes the
    per-plugin predicate over every pin. A TagVisibilityError from an
    anchored pin in a fully tagless checkout reads as "not a window"
    here; _require_or_skip then fails loudly under the require flag and
    skips locally, which is this file's documented taglessness contract.
    Every other check asks plugin_in_release_window directly, where the
    sentinel's raise PROPAGATES (S6: a blind checkout must never produce
    a green channel verdict).
    """
    from plugin_channel import TagVisibilityError

    try:
        return all(
            plugin_in_release_window(name, ref)
            for name, ref in _pinned_refs().items()
        )
    except TagVisibilityError:
        return False


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


def test_every_drifted_file_is_declared_in_the_ledger() -> None:
    """Undeclared drift is the failure -- drift itself is not.

    Judged per plugin (nexus-a2wmi.4): a plugin in its release window has
    zero drift by construction — its ref IS the content this branch cuts
    — so its contract reduces to "its ledger entries are empty" (the pin
    advance ships them). Every other plugin is checked strictly against
    its own ref over its own surface (.14's scoping).
    """
    _require_or_skip()
    drifted: set[str] = set()
    for plugin, ref in _pinned_refs().items():
        if plugin_in_release_window(plugin, ref):
            assert not _declared_paths_for(plugin), (
                f"{plugin} is in its release window (ref {ref!r} is the tag "
                f"this branch cuts) but the PENDING_RELEASE ledger still "
                f"lists its files — the pin advance ships them, so its "
                f"entries must be emptied in the cut commit."
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
    for plugin, ref in _pinned_refs().items():
        if plugin_in_release_window(plugin, ref):
            assert not _declared_paths_for(plugin), (
                f"{plugin} is in its release window: its ledger entries must "
                f"be EMPTY (see test_every_drifted_file_is_declared_in_the_"
                f"ledger)."
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

    def _fetch_step(self) -> dict:
        steps = self._workflow()["jobs"]["ledger"]["steps"]
        matches = [
            s for s in steps if "Fetch exactly the pinned" in s.get("name", "")
        ]
        assert len(matches) == 1, "the tag-fetch step is gone or renamed"
        return matches[0]

    def test_the_fetch_step_fetches_all_tags(self) -> None:
        """RDR-197 (nexus-a2wmi.4): the BASE CLIENT TAG must be fetched even
        when no pinned ref names it (the both-anchored state) — it is the
        blind-checkout sentinel and the wheel-surface proof's range base."""
        assert "git fetch --depth=1 --tags --force origin" in self._fetch_step()["run"], (
            "the explicit shallow --tags fetch is gone: in the both-anchored "
            "state the base client tag is never fetched, the sentinel raises, "
            "and every anchored evaluation goes dark (S6 for every plugin)."
        )

    def test_the_fetch_step_fetches_the_pr_head_sha(self) -> None:
        """The proof's fallback target on a cut PR is the PR head commit;
        actions/checkout resolves the merge ref at depth 1, so the head sha
        must be fetched explicitly, guarded to pull_request events."""
        step = self._fetch_step()
        env = step.get("env", {})
        assert "PR_HEAD_SHA" in env, "the PR head sha env plumbing is gone"
        assert "github.event_name == 'pull_request'" in str(env["PR_HEAD_SHA"])
        assert 'git fetch --depth=1 origin "$PR_HEAD_SHA"' in step["run"]

    def test_the_fetch_step_tolerates_the_anchored_shape(self) -> None:
        """Site 2 of the window (nexus-a2wmi.4): the per-ref bash loop must
        accept plugin-v<pyproject>-<n> for the current version on a cut PR,
        keeping the hard exit for every other unresolvable ref."""
        run = self._fetch_step()["run"]
        assert 'grep -qE "^plugin-v${version_re}-[1-9][0-9]*$"' in run, (
            "the anchored-shape tolerance is gone from the fetch loop: every "
            "plugin-cut PR hard-exits at the fetch step."
        )


# ---------------------------------------------------------------------------
# RDR-197 (nexus-a2wmi.1): the tag-anchored wheel-surface proof.
#
# A plugin cut ships plugin-tree content ONLY. The proof diffs the base
# client tag against the cut's target (the anchored tag when it resolves,
# else the cut PR's head commit — the ANCHORING rule in
# scripts/plugin_channel.py's docstring) and fails on any path outside the
# channel allowlist. Never a merge base as target (the fork point carries
# none of the cut's content — vacuous proof), never a development branch's
# HEAD (reports offenders the cut never shipped).
# ---------------------------------------------------------------------------


def test_plugin_tag_leaves_wheel_surface_untouched() -> None:
    """For every plugin pinned at an anchored ref, the range from the base
    client tag to the anchored tag touches only channel-allowlisted paths.

    Passes trivially while no plugin is pinned anchored (today's steady
    state); the fixture arm below is the non-vacuity proof of the
    mechanism, so that pass is honest, not a masked gate.
    """
    from plugin_channel import parse_plugin_tag, wheel_surface_offenders

    anchored = {
        name: parsed
        for name, ref in _pinned_refs().items()
        if (parsed := parse_plugin_tag(ref)) is not None
    }
    if not anchored:
        # PASS, not skip: with no anchored pin there is genuinely nothing
        # to prove, and the fixture arm below is the non-vacuity backstop.
        # The gate workflow hard-fails on ANY skip (nexus-05m1i vacuity
        # belt), so a skip here turned develop red on the first CI run.
        return
    for name, (version, n) in sorted(anchored.items()):
        offenders = wheel_surface_offenders(
            f"v{version}", f"plugin-v{version}-{n}", cwd=REPO_ROOT
        )
        assert offenders == [], (
            f"plugin {name}'s cut plugin-v{version}-{n} touches wheel-surface "
            f"paths: {offenders}. A plugin cut may never alter wheel content."
        )


def _cut_fixture_repo(tmp_path: pathlib.Path) -> tuple[pathlib.Path, str]:
    """A repo with a tagged base and a cut commit that violates the surface.

    Returns (repo, cut head sha). The proof target is the resolved head
    sha — the cut PR's head commit per the anchoring rule — never a bare
    HEAD and never a merge base.
    """
    repo = tmp_path / "cutrepo"
    repo.mkdir()

    def run(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        )

    run("init", "-q", "-b", "main")
    run("config", "user.email", "test@test.invalid")
    run("config", "user.name", "test")
    (repo / "seed").write_text("seed\n", encoding="utf-8")
    run("add", "seed")
    run("commit", "-q", "-m", "seed")
    run("tag", "v0.0.1")
    for rel in (
        "src/nexus/cli.py",
        "conexus/plans/builtin/x.yml",
        "conexus/skills/ok.md",
        "sn/commands/ok.md",
    ):
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("cut content\n", encoding="utf-8")
        run("add", rel)
    run("commit", "-q", "-m", "cut")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return repo, head


def test_the_wheel_surface_proof_reports_offenders(tmp_path: pathlib.Path) -> None:
    """Non-vacuity arm: a range carrying wheel content MUST fail the proof.

    src/nexus/cli.py is off-allowlist outright; conexus/plans/builtin/x.yml
    is inside the allowlist prefix but carved out by DENIED_PREFIXES (it is
    wheel package data). Empty DENIED_PREFIXES and this test fails — the
    designed falsification.
    """
    from plugin_channel import wheel_surface_offenders

    repo, cut_sha = _cut_fixture_repo(tmp_path)
    offenders = wheel_surface_offenders("v0.0.1", cut_sha, cwd=repo)
    assert offenders == ["conexus/plans/builtin/x.yml", "src/nexus/cli.py"]


def test_the_proof_raises_when_the_target_does_not_resolve(
    tmp_path: pathlib.Path,
) -> None:
    """An unresolvable ref is an unknown answer, never an empty offender list."""
    from plugin_channel import GitCommandError, wheel_surface_offenders

    repo, _ = _cut_fixture_repo(tmp_path)
    with pytest.raises(GitCommandError):
        wheel_surface_offenders("v0.0.1", "does-not-resolve", cwd=repo)


def test_no_caller_passes_a_merge_base_as_the_proof_target() -> None:
    """No caller in this repo hands wheel_surface_offenders a merge-base
    target. A merge base is only ever a range BASE; as a target the proof
    is vacuous (the fork point carries none of the cut's content).

    Scans the realistic caller surfaces — tests/, scripts/ (python, via
    ast so docstrings do not false-positive) and .github/workflows/ (text
    lines) — and fails if any call's target argument mentions a merge
    base. Non-vacuity: asserts the scan saw at least one python call site
    (this file has two above).
    """
    import ast

    violations: list[str] = []
    python_call_sites = 0
    for directory in ("tests", "scripts"):
        for path in sorted((REPO_ROOT / directory).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = (
                    func.attr
                    if isinstance(func, ast.Attribute)
                    else getattr(func, "id", "")
                )
                if name != "wheel_surface_offenders":
                    continue
                python_call_sites += 1
                target: ast.expr | None = None
                if len(node.args) >= 2:
                    target = node.args[1]
                for keyword in node.keywords:
                    if keyword.arg == "target_ref":
                        target = keyword.value
                rendered = ast.unparse(target) if target is not None else ""
                if "merge" in rendered.lower():
                    violations.append(f"{path}: target {rendered!r}")
    workflows = REPO_ROOT / ".github" / "workflows"
    if workflows.is_dir():
        for path in sorted(workflows.glob("*.yml")):
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if (
                    "wheel_surface_offenders" in line or "plugin_channel" in line
                ) and ("merge-base" in line or "merge_base" in line):
                    violations.append(f"{path}:{line_number}: {line.strip()}")
    assert python_call_sites >= 1, (
        "the scan recognised zero wheel_surface_offenders call sites — the "
        "scan itself is broken, which is not a pass"
    )
    assert not violations, violations


class TestPerPluginDriftScoping:
    """RDR-197 (nexus-a2wmi.14): drift is judged per plugin, ref by ref.

    After a conexus-only cut, sn's ref still points at the older client
    tag. Diffing that older ref against HEAD over the FLATTENED surface
    returns the conexus files the cut just shipped; the cut emptied the
    ledger, so those paths read as undeclared drift and the contract
    fails on develop — caused by the channel working correctly. The fix
    is scoping each plugin's diff to its own surface, and these tests pin
    it from both directions: the cut must not fail the other plugin, and
    scoping must not blind the check to that plugin's genuine drift.

    This is row 4 / state S3 of the mixed-state truth table (bead .4).
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
        """A repo one conexus cut past the shared client tag.

        History: baseline (tag v9.9.0, both plugins pinned there) → the
        conexus cut (a conexus/hooks/ change + conexus's pin advanced to
        plugin-v9.9.0-1, tagged) → post-cut develop (a conexus/commands/
        change, DECLARED in the ledger; with *sn_drift*, an UNDECLARED
        sn/hooks/ change too). The production test functions then run
        against this world via the module globals.
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

        write("conexus/hooks/hook.py", "v2 shipped by the cut\n")
        write(".claude-plugin/marketplace.json", marketplace("plugin-v9.9.0-1"))
        run("add", ".")
        run("commit", "-q", "-m", "conexus cut 1")
        run("tag", "plugin-v9.9.0-1")

        write("conexus/commands/cmd.md", "v2 post-cut work\n")
        write(
            "conexus/PENDING_RELEASE.md",
            "# Pending\n- `conexus/commands/cmd.md`: post-cut change (fixture)\n",
        )
        if sn_drift:
            write(cls.SN_PROBE, "v2 UNDECLARED\n")
        run("add", ".")
        run("commit", "-q", "-m", "post-cut develop")

        module = sys.modules[__name__]
        monkeypatch.setattr(module, "REPO_ROOT", repo)
        monkeypatch.setattr(
            module, "MARKETPLACE", repo / ".claude-plugin" / "marketplace.json"
        )
        monkeypatch.setattr(
            module, "LEDGER", repo / "conexus" / "PENDING_RELEASE.md"
        )
        return repo

    def test_a_conexus_only_cut_reports_no_failure_for_sn(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The channel working correctly must not turn develop red.

        Restore the flattened-surface diff and this fails: sn's older ref
        picks up the cut's conexus files as undeclared drift.
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


# ---------------------------------------------------------------------------
# RDR-197 (nexus-a2wmi.4): window semantics — invariant W per plugin.
# The four conditions and the anchoring rule live in
# scripts/plugin_channel.py's docstring; nothing below restates them.
# ---------------------------------------------------------------------------


class TestAnchoredWindowBehaviour:
    """plugin_in_release_window, one condition isolated per test.

    Mirrors TestTaglessBehaviour's monkeypatch style: every collaborator
    is stubbed except the condition under test, so a failure names the
    condition rather than the fixture.
    """

    REF = "plugin-v9.9.0-1"

    @pytest.fixture(autouse=True)
    def _stub_world(self, monkeypatch: pytest.MonkeyPatch):
        import plugin_channel

        module = sys.modules[__name__]
        monkeypatch.setattr(module, "_pyproject_version", lambda: "9.9.0")
        # Tag does not resolve locally (the window's precondition).
        monkeypatch.setattr(
            module,
            "_git",
            lambda *args: subprocess.CompletedProcess(args, 1, stdout="", stderr=""),
        )
        monkeypatch.setattr(module, "_remote_confirms_tag_absent", lambda ref: True)
        monkeypatch.setattr(
            plugin_channel, "next_plugin_tag_number", lambda v, *, cwd=None: 1
        )
        monkeypatch.setattr(
            plugin_channel, "is_cut_branch_for", lambda v, n, *, cwd=None: True
        )
        self.monkeypatch = monkeypatch
        self.module = module

    def test_all_four_conditions_grant_the_window(self) -> None:
        assert plugin_in_release_window("conexus", self.REF) is True

    def test_a_resolving_tag_is_never_a_window(self) -> None:
        self.monkeypatch.setattr(
            self.module,
            "_git",
            lambda *args: subprocess.CompletedProcess(args, 0, stdout="ok", stderr=""),
        )
        assert plugin_in_release_window("conexus", self.REF) is False

    def test_condition_a_wrong_version_refused(self) -> None:
        assert plugin_in_release_window("conexus", "plugin-v9.8.0-1") is False

    def test_condition_b_number_mismatch_refused(self) -> None:
        import plugin_channel

        self.monkeypatch.setattr(
            plugin_channel, "next_plugin_tag_number", lambda v, *, cwd=None: 2
        )
        assert plugin_in_release_window("conexus", self.REF) is False

    def test_condition_c_upstream_present_refused(self) -> None:
        self.monkeypatch.setattr(
            self.module, "_remote_confirms_tag_absent", lambda ref: False
        )
        assert plugin_in_release_window("conexus", self.REF) is False

    def test_condition_c_inconclusive_probe_fails_closed(self) -> None:
        self.monkeypatch.setattr(
            self.module, "_remote_confirms_tag_absent", lambda ref: None
        )
        assert plugin_in_release_window("conexus", self.REF) is False

    def test_condition_d_wrong_branch_refused(self) -> None:
        import plugin_channel

        self.monkeypatch.setattr(
            plugin_channel, "is_cut_branch_for", lambda v, n, *, cwd=None: False
        )
        assert plugin_in_release_window("conexus", self.REF) is False

    def test_client_form_window_is_tag_absence(self) -> None:
        assert plugin_in_release_window("sn", "v9.9.0") is True

    def test_client_form_with_existing_tag_is_no_window(self) -> None:
        self.monkeypatch.setattr(
            self.module,
            "_git",
            lambda *args: subprocess.CompletedProcess(args, 0, stdout="ok", stderr=""),
        )
        assert plugin_in_release_window("sn", "v9.9.0") is False


class TestReleaseWindowComposite:
    """The composite _in_release_window's REAL try/except body (R1 review
    finding, a2wmi.6): every other test stubs the composite itself, so its
    TagVisibilityError-to-False conversion had no direct coverage. Only
    the per-plugin predicate is stubbed here — the body under test runs."""

    def _pins(self, monkeypatch, predicate) -> None:
        module = sys.modules[__name__]
        monkeypatch.setattr(
            module,
            "_pinned_refs",
            lambda: {"conexus": "plugin-v9.9.0-1", "sn": "v9.9.0"},
        )
        monkeypatch.setattr(module, "plugin_in_release_window", predicate)

    def test_all_windowed_composes_to_true(self, monkeypatch) -> None:
        self._pins(monkeypatch, lambda name, ref: True)
        assert _in_release_window() is True

    def test_one_unwindowed_composes_to_false(self, monkeypatch) -> None:
        self._pins(monkeypatch, lambda name, ref: ref != "v9.9.0")
        assert _in_release_window() is False

    def test_a_blind_checkout_reads_as_not_a_window(self, monkeypatch) -> None:
        """TagVisibilityError converts to False HERE ONLY — the tagless
        path, where _require_or_skip then fails loudly under the require
        flag and skips locally. Everywhere else the sentinel's raise
        propagates (S6)."""
        from plugin_channel import TagVisibilityError

        def blind(name: str, ref: str) -> bool:
            raise TagVisibilityError("base client tag does not resolve")

        self._pins(monkeypatch, blind)
        assert _in_release_window() is False

    def test_any_other_exception_propagates(self, monkeypatch) -> None:
        """The conversion is scoped to TagVisibilityError alone; a broader
        except would silently misread real git failures as not-a-window."""

        def broken(name: str, ref: str) -> bool:
            raise RuntimeError("unrelated failure")

        self._pins(monkeypatch, broken)
        with pytest.raises(RuntimeError, match="unrelated failure"):
            _in_release_window()


def test_the_tagless_probe_targets_the_pinned_refs(monkeypatch) -> None:
    """Site 3 (nexus-a2wmi.4): the upstream probe checks the refs ACTUALLY
    pinned, never a reconstructed "v" + pyproject string — which checks
    the wrong ref the moment any plugin pins the anchored form."""
    module = sys.modules[__name__]
    probed: list[str] = []
    monkeypatch.delenv(_REQUIRE_ENV, raising=False)
    monkeypatch.setattr(module, "_has_any_tags", lambda: False)
    monkeypatch.setattr(module, "_in_release_window", lambda: True)
    monkeypatch.setattr(
        module,
        "_pinned_refs",
        lambda: {"conexus": "plugin-v9.9.0-1", "sn": "v9.9.0"},
    )

    def recording(ref: str) -> bool:
        probed.append(ref)
        return True

    monkeypatch.setattr(module, "_remote_confirms_tag_absent", recording)
    _require_or_skip()  # window confirmed: returns
    assert sorted(probed) == ["plugin-v9.9.0-1", "v9.9.0"]


# ---------------------------------------------------------------------------
# The mixed-state truth table (bead .4 step 8): one case per cell.
# Row 1 (parity) lives in tests/test_plugin_structure.py (bead .3); row 7
# (workflow bash tolerance) is the wiring tests on TestTheCIWiringItself.
# Everything else runs here against fixture worlds. States:
#   S1 pre-cut · S2 cut-PR (clean / dirty / far-future-n) · S3 post-cut
#   develop · S4 post-client-release · S5 dangling (the closer's state) ·
#   S6 blind checkout (stray tag fetched, base client tag absent).
# ---------------------------------------------------------------------------


def _channel_state(
    state: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> dict:
    pv = "9.10.0" if state == "S4" else "9.9.0"
    repo = tmp_path / "world"
    repo.mkdir()

    def run(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.strip()

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
                        "source": {"source": "git-subdir", "ref": f"v{pv}"},
                    },
                ]
            }
        )

    n = 7 if state == "S2far" else 1
    anchored_ref = f"plugin-v{pv}-{n}"

    run("init", "-q", "-b", "main")
    run("config", "user.email", "test@test.invalid")
    run("config", "user.name", "test")
    write("conexus/hooks/hook.py", "v1\n")
    write("conexus/commands/cmd.md", "v1\n")
    write("sn/hooks/probe.py", "v1\n")
    write(
        ".claude-plugin/marketplace.json",
        marketplace(anchored_ref if state == "S6" else f"v{pv}"),
    )
    write("conexus/PENDING_RELEASE.md", "# Pending\n")
    write("pyproject.toml", f'[project]\nname = "w"\nversion = "{pv}"\n')
    run("add", ".")
    run("commit", "-q", "-m", "baseline")
    if state == "S6":
        run("tag", "unrelated-old-tag")  # SOME tags visible; the base is not
    else:
        run("tag", f"v{pv}")

    pr_head: str | None = None
    if state in ("S2", "S2dirty", "S2far", "S3", "S5"):
        run("checkout", "-q", "-b", f"plugin-release/{pv}-{n}")
        write("conexus/hooks/hook.py", "v2 shipped by the cut\n")
        write(".claude-plugin/marketplace.json", marketplace(anchored_ref))
        if state == "S2dirty":
            write("src/nexus/cli.py", "wheel content in the cut\n")
        run("add", ".")
        run("commit", "-q", "-m", "conexus cut")
        pr_head = run("rev-parse", "HEAD")
    if state == "S3":
        run("tag", anchored_ref)
    if state in ("S3", "S5"):
        run("checkout", "-q", "main")
        run("merge", "-q", "--ff-only", f"plugin-release/{pv}-{n}")
    if state == "S3":
        write("conexus/commands/cmd.md", "v2 post-cut work\n")
        write(
            "conexus/PENDING_RELEASE.md",
            "# Pending\n- `conexus/commands/cmd.md`: post-cut change (fixture)\n",
        )
        write("src/nexus/offwheel.py", "develop accumulation off the allowlist\n")
        run("add", ".")
        run("commit", "-q", "-m", "post-cut develop")

    module = sys.modules[__name__]
    monkeypatch.setattr(module, "REPO_ROOT", repo)
    monkeypatch.setattr(
        module, "MARKETPLACE", repo / ".claude-plugin" / "marketplace.json"
    )
    monkeypatch.setattr(module, "LEDGER", repo / "conexus" / "PENDING_RELEASE.md")
    monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
    # No real origin in a fixture world; condition (c) is stubbed CONFIRMED
    # so the other conditions are what each cell isolates. Condition (c)'s
    # own behaviour is TestAnchoredWindowBehaviour's.
    monkeypatch.setattr(module, "_remote_confirms_tag_absent", lambda ref: True)
    monkeypatch.setenv(_REQUIRE_ENV, "1")  # S6 must be LOUD, never a local skip
    return {"repo": repo, "pv": pv, "n": n, "pr_head": pr_head, "ref": anchored_ref}


def _cell_r2_window(state: str, world: dict) -> None:
    from plugin_channel import TagVisibilityError

    refs = _pinned_refs()
    if state == "S2":
        assert plugin_in_release_window("conexus", refs["conexus"]) is True
        assert plugin_in_release_window("sn", refs["sn"]) is False
    elif state == "S2far":
        # Condition (b): next is 1, the ref names 7. Delete (b) and this
        # cell grants a window to a nonexistent future tag.
        assert plugin_in_release_window("conexus", refs["conexus"]) is False
    elif state == "S5":
        assert plugin_in_release_window("conexus", refs["conexus"]) is False
    elif state == "S6":
        with pytest.raises(TagVisibilityError):
            plugin_in_release_window("conexus", refs["conexus"])
    else:  # S1, S3, S4: every tag resolves, no plugin is in a window
        for name, ref in refs.items():
            assert plugin_in_release_window(name, ref) is False, (name, ref)


def _cell_r3_real_tag(state: str, world: dict) -> None:
    from plugin_channel import TagVisibilityError

    if state == "S5":
        with pytest.raises(AssertionError, match="does not exist"):
            test_every_pinned_ref_is_a_real_tag()
    elif state == "S6":
        with pytest.raises(TagVisibilityError):
            test_every_pinned_ref_is_a_real_tag()
    else:
        test_every_pinned_ref_is_a_real_tag()


def _cell_r45_ledger_contract(state: str, world: dict) -> None:
    from plugin_channel import TagVisibilityError

    if state == "S5":
        with pytest.raises(AssertionError, match="drift is UNKNOWN"):
            test_every_drifted_file_is_declared_in_the_ledger()
    elif state == "S6":
        with pytest.raises(TagVisibilityError):
            test_every_drifted_file_is_declared_in_the_ledger()
    else:
        test_every_drifted_file_is_declared_in_the_ledger()
        test_the_ledger_has_no_stale_entries()


def _cell_r8_proof(state: str, world: dict) -> None:
    from plugin_channel import GitCommandError, wheel_surface_offenders

    base = f"v{world['pv']}"
    repo = world["repo"]
    if state in ("S2", "S2far"):
        # Target: the cut PR's HEAD COMMIT (the tag does not exist yet).
        assert wheel_surface_offenders(base, world["pr_head"], cwd=repo) == []
    elif state == "S2dirty":
        # Changing this target to a merge base returns [] (the fork point
        # carries none of the cut) and this cell fails — by design.
        assert wheel_surface_offenders(base, world["pr_head"], cwd=repo) == [
            "src/nexus/cli.py"
        ]
    elif state == "S3":
        # Target: the anchored tag — a fixed historical range that stays
        # empty no matter what develop accumulates (the fixture's post-cut
        # commit adds src/nexus/offwheel.py; a development-HEAD target
        # would report it and this cell would fail — by design).
        assert wheel_surface_offenders(base, world["ref"], cwd=repo) == []
    elif state == "S5":
        with pytest.raises(GitCommandError):
            wheel_surface_offenders(base, world["ref"], cwd=repo)
    elif state == "S6":
        with pytest.raises(GitCommandError):
            wheel_surface_offenders(base, world["ref"], cwd=repo)
    # S1 / S4: no anchored ref — the proof does not gate.


def _cell_r9_admission(state: str, world: dict) -> None:
    from plugin_channel import (
        current_branch_name,
        next_plugin_tag_number,
        parse_plugin_tag,
    )

    repo = world["repo"]
    ref = world["ref"]
    parsed = parse_plugin_tag(ref)
    assert parsed == (world["pv"], world["n"])  # condition (a)
    assert next_plugin_tag_number(world["pv"], cwd=repo) == world["n"]  # (b)
    # Condition (c) is the stubbed upstream probe (see _channel_state).
    branch = current_branch_name(cwd=repo)
    if state == "S2":
        assert branch == f"plugin-release/{world['pv']}-{world['n']}"  # (d)
        assert plugin_in_release_window("conexus", ref) is True
    else:  # S5: conditions a-c hold and ONLY the branch fails
        assert branch == "main"
        assert plugin_in_release_window("conexus", ref) is False


_TRUTH_TABLE_CELLS = [
    ("r2-window-S1", "S1", _cell_r2_window),
    ("r2-window-S2", "S2", _cell_r2_window),
    ("r2-window-S2far", "S2far", _cell_r2_window),
    ("r2-window-S3", "S3", _cell_r2_window),
    ("r2-window-S4", "S4", _cell_r2_window),
    ("r2-window-S5", "S5", _cell_r2_window),
    ("r2-window-S6", "S6", _cell_r2_window),
    ("r3-real-tag-S1", "S1", _cell_r3_real_tag),
    ("r3-real-tag-S2", "S2", _cell_r3_real_tag),
    ("r3-real-tag-S3", "S3", _cell_r3_real_tag),
    ("r3-real-tag-S4", "S4", _cell_r3_real_tag),
    ("r3-real-tag-S5", "S5", _cell_r3_real_tag),
    ("r3-real-tag-S6", "S6", _cell_r3_real_tag),
    ("r45-ledger-S1", "S1", _cell_r45_ledger_contract),
    ("r45-ledger-S2", "S2", _cell_r45_ledger_contract),
    ("r45-ledger-S3", "S3", _cell_r45_ledger_contract),
    ("r45-ledger-S4", "S4", _cell_r45_ledger_contract),
    ("r45-ledger-S5", "S5", _cell_r45_ledger_contract),
    ("r45-ledger-S6", "S6", _cell_r45_ledger_contract),
    ("r8-proof-S2", "S2", _cell_r8_proof),
    ("r8-proof-S2dirty", "S2dirty", _cell_r8_proof),
    ("r8-proof-S3", "S3", _cell_r8_proof),
    ("r8-proof-S5", "S5", _cell_r8_proof),
    ("r8-proof-S6", "S6", _cell_r8_proof),
    ("r9-admission-S2", "S2", _cell_r9_admission),
    ("r9-admission-S5", "S5", _cell_r9_admission),
]


@pytest.mark.parametrize(
    ("state", "check"),
    [pytest.param(state, check, id=cell_id) for cell_id, state, check in _TRUTH_TABLE_CELLS],
)
def test_mixed_state_truth_table(
    state: str,
    check,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One case per cell of the RDR-197 mixed-state truth table (bead .4).

    Row 6 (probe target) is test_the_tagless_probe_targets_the_pinned_refs;
    row 10 (blind-checkout loudness) is the S6 column of rows 2, 3, 4/5
    and 8 — every one of them raises rather than reporting green.
    """
    world = _channel_state(state, tmp_path, monkeypatch)
    check(state, world)


@pytest.fixture(autouse=True)
def _pin_t2_substrate() -> None:
    """Shadow conftest's autouse engine pin: these tests read files and git only.

    Without this, the session-wide ``_pin_t2_substrate`` boots an engine the
    file never touches, and on a box with a stale or absent service jar every
    test here errors at setup and executes nothing -- 65 errors in 1.83s on
    2026-08-27 (nexus-i0wsm), on exactly the machine that has an installed
    plugin to check. A closer-scope fixture of the same name wins the lookup.
    """
