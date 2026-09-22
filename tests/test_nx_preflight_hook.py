# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-hwbj (GH #619): where the preflight hook sits in SessionStart.

The preflight's own contract -- silent when ``nx --version`` works, a loud
``## nx Preflight: FAILED`` marker when it does not, always exit 0 -- moved
to ``tests/hooks/test_preflight_verb.py`` with the RDR-215 port, and the
plugin script those assertions used to drive is deleted.

What stays here is the part that is about hooks.json rather than about any
implementation: the preflight runs SECOND, after ``nx-hook upgrade-auto``
and before the guidance emission, so a FAILED marker lands ABOVE the routing
it counter-signals rather than below it.
"""
from __future__ import annotations

import json
from pathlib import Path


def _invocations(sessionstart: list[dict]) -> list[str]:
    """Each SessionStart hook as one string, whatever form it is declared in.

    RDR-215 nexus-q02nx.21 moved the preflight from a command line naming
    ``preflight.py`` to the exec form ``{"command": "nx-hook", "args":
    ["preflight"]}``. The ORDER contract these tests pin is unchanged and
    still matters; only the spelling moved, so they match on the rendered
    invocation rather than on ``command`` alone.
    """
    out = []
    for h in sessionstart:
        parts = [h.get("command", "")]
        parts.extend(a for a in h.get("args", []) if isinstance(a, str))
        out.append(" ".join(p for p in parts if p))
    return out


class TestHookConfigWiresPreflightEarly:
    """Preflight must run AFTER the ``nx upgrade --auto`` self-
    upgrade (test_phase5_integration.TestHooksJson asserts that
    upgrade is the first hook, since stale-conexus version
    handling is a hard prereq for everything else) but BEFORE the
    cat-of-using-nx-skills step. Position 2 keeps the FAILED
    marker (if any) above the 600-line capability dump and the
    routing skill so the model sees the counter-signal first.
    """

    def test_hook_config_references_preflight_second(self) -> None:
        cfg = (
            Path(__file__).resolve().parent.parent
            / "conexus" / "hooks" / "hooks.json"
        )
        data = json.loads(cfg.read_text())
        sessionstart = data["hooks"]["SessionStart"][0]["hooks"]
        # Position 0 is `nx upgrade --auto` (existing contract per
        # tests/test_phase5_integration.py::TestHooksJson::
        # test_upgrade_auto_is_first_session_start_hook).
        cmds = _invocations(sessionstart)
        # nexus-q02nx.22 moved this one too: `nx upgrade --auto 2>/dev/null
        # || echo ... >&2` is now `nx-hook upgrade-auto`, with the redirect
        # and the fallback inside the verb. The ORDER contract is unchanged.
        assert "nx-hook upgrade-auto" == cmds[0]
        # Position 1 must be the preflight so the FAILED marker
        # lands above the capability dump and the using-nx-skills
        # routing.
        assert "nx-hook preflight" == cmds[1], (
            f"preflight must be the SECOND SessionStart hook (right "
            f"after `nx upgrade --auto`). Got hook[1]: {cmds[1]!r}"
        )

    def test_hook_config_preflight_runs_before_guidance_emission(self) -> None:
        """The guidance emission (`nx-hook session-start`, the
        nexus-h33x8.4 wheel channel that replaced the using-nx-skills
        cat entry) must come AFTER the preflight so the FAILED
        counter-signal appears above the routing it counters.
        """
        cfg = (
            Path(__file__).resolve().parent.parent
            / "conexus" / "hooks" / "hooks.json"
        )
        data = json.loads(cfg.read_text())
        sessionstart = data["hooks"]["SessionStart"][0]["hooks"]
        cmds = _invocations(sessionstart)
        preflight_idx = next(
            (i for i, c in enumerate(cmds) if "nx-hook preflight" in c), -1,
        )
        guidance_idx = next(
            (i for i, c in enumerate(cmds) if "nx-hook session-start" in c), -1,
        )
        assert preflight_idx >= 0, "preflight hook missing"
        assert guidance_idx >= 0, (
            "guidance emission hook (`nx-hook session-start`) missing — "
            "the nexus-h33x8.4 channel that replaced the using-nx-skills "
            "cat entry"
        )
        assert preflight_idx < guidance_idx, (
            f"preflight (index {preflight_idx}) must run before the "
            f"guidance emission (index {guidance_idx}); otherwise "
            f"the FAILED marker lands AFTER the routing it "
            f"counters and the model sees the routing first."
        )

    def test_hook_config_emits_guidance_without_legacy_cat(self) -> None:
        """The using-nx-skills routing is still injected — now via
        `nx-hook session-start` (nexus-h33x8.4: GUIDANCE_IMPERATIVE in
        the wheel, session-cadence) — and the legacy `cat SKILL.md`
        entry must stay REMOVED. Re-adding the cat would lean on the
        interim legacy_cat_channel_active() self-suppression seam
        instead of the intended end-state (single wheel channel), and
        would re-freeze guidance edits to plugin-release cadence.
        The preflight FAILED marker remains a counter-signal, not a
        replacement, so the emission hook itself must stay present.
        """
        cfg = (
            Path(__file__).resolve().parent.parent
            / "conexus" / "hooks" / "hooks.json"
        )
        data = json.loads(cfg.read_text())
        sessionstart = data["hooks"]["SessionStart"][0]["hooks"]
        # `command` ALONE IS NOT THE INVOCATION any more. Under exec form
        # the verb lives in `args`, so a `h["command"]` join renders this
        # entry as the bare word `nx-hook` and the assertion below would
        # look for a substring that cannot appear — passing or failing on
        # the spelling rather than on the wiring. _invocations() renders
        # both halves; that is what these assertions are about.
        commands = " ".join(_invocations(sessionstart))
        assert "nx-hook session-start" in commands, (
            "guidance emission channel missing from SessionStart"
        )
        assert "using-nx-skills" not in commands, (
            "legacy using-nx-skills cat entry resurrected in "
            "hooks.json — nexus-h33x8.4 removed it deliberately; "
            "guidance ships via `nx-hook session-start` (wheel)"
        )
