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

#: Flattened, for the checks that do not care which plugin a path belongs to.
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


def _drifted_paths(ref: str) -> list[str]:
    proc = _git("diff", "--name-only", f"{ref}..HEAD", "--", *SURFACE)
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
        confirmed = _remote_confirms_tag_absent(f"v{_pyproject_version()}")
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


def _in_release_window() -> bool:
    """True during the release-PR window: every pinned ref is ``v<X.Y.Z>``
    where X.Y.Z equals pyproject's version AND the tag does not exist yet.

    The release checklist bumps ``source.ref`` to the ABOUT-TO-BE-CUT tag on
    the release branch; the tag is created only after the PR merges (skill
    step 9), so on that branch — and for the seconds between merge-push and
    tag-push — the ref legitimately names a not-yet-existing tag. In that
    window the working tree IS the tag's future content: the ref-exists check
    passes, and the drift contract reduces to "the pending ledger is EMPTY"
    (advancing the pin is what empties it; checklist step 3). Outside the
    window (versions differ, or the tag exists) every check is strict — a
    stale ref pointing at a never-created tag still fails, which is the
    botched-release case this gate exists for. First exercised at the 7.0.0
    cut (2026-08-01), the first release since these gates were built.
    """
    import tomllib

    with open(REPO_ROOT / "pyproject.toml", "rb") as fh:
        version = tomllib.load(fh)["project"]["version"]
    for ref in set(_pinned_refs().values()):
        if ref != f"v{version}":
            return False
        if _git("rev-parse", "--verify", f"{ref}^{{commit}}").returncode == 0:
            return False
    return True


def test_every_pinned_ref_is_a_real_tag() -> None:
    _require_or_skip()
    if _in_release_window():
        return  # ref names the tag this very branch cuts; created at merge
    for name, ref in sorted(_pinned_refs().items()):
        proc = _git("rev-parse", "--verify", f"{ref}^{{commit}}")
        assert proc.returncode == 0, (
            f"marketplace.json pins {name} at source.ref={ref!r}, but that tag "
            f"does not exist. Installed users resolve the plugin from that ref, "
            f"so it must be a real, pushed tag."
        )


# --- the ledger contract, both directions off ONE parsed set ----------------


def test_every_drifted_file_is_declared_in_the_ledger() -> None:
    """Undeclared drift is the failure -- drift itself is not."""
    _require_or_skip()
    if _in_release_window():
        # The tree IS the future tag's content: drift is zero by
        # construction and the contract reduces to "the ledger is empty"
        # (checklist step 3 empties it when the pin advances).
        assert not _declared_paths(), (
            "release window (pin == pyproject, tag not yet cut) but the "
            "PENDING_RELEASE ledger still lists entries — the pin advance "
            "ships them, so the list must be emptied in the release commit."
        )
        return
    declared = _declared_paths()
    drifted: set[str] = set()
    for ref in set(_pinned_refs().values()):
        drifted |= set(_drifted_paths(ref))
    if not drifted:
        return

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
    if _in_release_window():
        assert not _declared_paths(), (
            "release window: the ledger must be EMPTY (see "
            "test_every_drifted_file_is_declared_in_the_ledger)."
        )
        return
    declared = _declared_paths()
    drifted: set[str] = set()
    for ref in set(_pinned_refs().values()):
        drifted |= set(_drifted_paths(ref))

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

    Skips while no plugin is pinned anchored (today's steady state); the
    fixture arm below is the non-vacuity proof of the mechanism, so this
    skip is honest, not a masked gate.
    """
    from plugin_channel import parse_plugin_tag, wheel_surface_offenders

    anchored = {
        name: parsed
        for name, ref in _pinned_refs().items()
        if (parsed := parse_plugin_tag(ref)) is not None
    }
    if not anchored:
        pytest.skip("no plugin pinned at an anchored ref; fixture arm covers the mechanism")
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
