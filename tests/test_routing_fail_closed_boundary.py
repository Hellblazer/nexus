# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The fail-closed BOUNDARY, not the hook bodies (RDR-215 bead nexus-q02nx.21).

``tests/test_routing_phase_review_close.py`` drives the hook script through
its nineteen scenarios, and every one of them reaches a decision. None makes
``body()`` RAISE, so nothing there exercises ``_lib.run_hook``'s except
branch — the one place the fail-closed contract actually lives. Bead
nexus-q02nx.21's round-2 audit note names that gap exactly: "the current
tests exercise ``body()``, not the ``run_hook`` wrapper, so nothing today
would catch the inversion."

The inversion it means: a tier where a crashed hook reads as ALLOW. RDR-215
considered putting ``phase_review_close_requires_gate`` on the MCP tool
tier, where a raised exception returns empty text with ``isError`` false and
a disconnected server is a non-blocking error — both of which read as allow,
and a phase could then close without its gate. Sam ruled it onto the command
tier on 2026-09-18 for that reason.

These tests pin the property that ruling depends on, so that a future move
to any tier that cannot honour it fails here rather than in production.

**What the contract is, precisely** (``routing/README.md`` item 5): a rule is
fail-closed when BOTH ``run_hook(..., fail_closed=True)`` at the call site
AND ``fail_closed: true`` in ``registry.yaml`` hold. Two surfaces, edited by
hand, and until now nothing checked they agreed.
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

import pytest

ROUTING = (
    pathlib.Path(__file__).parent.parent
    / "conexus" / "hooks" / "scripts" / "routing"
)

#: Where a PORTED guard lives (nexus-t9klx). The two-surface contract this
#: module pins — registry.yaml and the call site must agree on fail_closed —
#: is not about which directory the guard sits in, so the call-site scan
#: covers both. Scanning only the plugin directory would have quietly
#: reported a ported rule as having no call site at all, which is how this
#: file first went red.
WHEEL_HOOKS = pathlib.Path(__file__).parent.parent / "src" / "nexus" / "hooks"

#: Drives ``_lib.run_hook`` with a body that raises, in a child process,
#: because ``run_hook``'s emitters call ``sys.exit`` and its stdout IS the
#: assertion. Nothing here imports the hook modules in-process.
#:
#: nexus-t9klx: a package import, not a ``sys.path`` insert into
#: ``routing/``. Both guards are in the wheel now and the plugin's copy of
#: the library is deleted. ``run_hook`` itself is deliberately still what
#: is driven, not ``run_hook_result``: this file pins the fail-closed
#: BOUNDARY — that a crashed guard still writes an envelope — and
#: ``run_hook`` is the exiting form where that is hardest to hold.
_DRIVER = """
from nexus.hooks import _routing_lib as _lib

def body(payload):
    raise RuntimeError("induced: the guard could not determine the state")

_lib.run_hook(body, fail_closed={fail_closed}, rule_name="induced_test_rule")
"""


def _drive(*, fail_closed: bool) -> tuple[int, dict]:
    proc = subprocess.run(
        [sys.executable, "-c", _DRIVER.format(fail_closed=fail_closed)],
        input="{}", capture_output=True, text=True, timeout=30,
    )
    assert proc.stdout.strip(), (
        f"run_hook emitted NOTHING on a raised body (rc={proc.returncode}). "
        f"Silence is the failure this file exists to catch.\n{proc.stderr}"
    )
    return proc.returncode, json.loads(proc.stdout)


class TestARaisedBodyStillDecides:
    """The mechanism Sam's 2026-09-18 ruling turns on.

    Note what is NOT asserted: a non-zero exit code. The bead's own
    correction says so — ``deny()`` writes a JSON envelope and exits 0, so
    the deny is envelope-encoded like every other decision in this layer.
    What the command tier buys is that the process is still alive to WRITE
    that envelope. Build to the envelope, not to the exit code.
    """

    def test_fail_closed_denies(self):
        rc, out = _drive(fail_closed=True)
        decision = out["hookSpecificOutput"]["permissionDecision"]
        assert decision == "deny", (
            f"a crashed fail-closed guard answered {decision!r}. That is the "
            "inversion: a phase could close without its gate."
        )
        assert rc == 0, "the deny is envelope-encoded; the exit code is 0"

    def test_fail_open_allows(self):
        """The other half, so the test above is not passing for some
        reason unrelated to the flag."""
        rc, out = _drive(fail_closed=False)
        assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert rc == 0

    def test_the_deny_reason_names_the_cause(self):
        """A bare deny with no reason is indistinguishable from a policy
        deny, and sends the reader hunting for a gate that was never the
        problem."""
        _rc, out = _drive(fail_closed=True)
        reason = json.dumps(out)
        assert "fail-closed" in reason
        assert "induced" in reason, (
            "the underlying exception text is dropped; a crashed guard "
            f"should say what crashed it. Got: {reason}"
        )


class TestTheTwoSurfacesAgree:
    """``registry.yaml`` and the call site must say the same thing.

    README item 5 states the contract as both together. They are two
    hand-edited files, and a rule that is ``fail_closed: true`` in the
    registry but ``fail_closed=False`` at its call site is silently
    fail-open — the registry would read as documentation of a guarantee
    that is not in force.
    """

    @staticmethod
    def _registry_flags() -> dict[str, bool]:
        """Parsed without PyYAML, which routing code may not import.

        The shape is fixed and shallow: two-space rule keys under
        ``rules:``, with ``fail_closed:`` four-space indented beneath one.
        """
        text = (ROUTING / "registry.yaml").read_text()
        flags: dict[str, bool] = {}
        current = None
        for line in text.splitlines():
            if m := re.match(r"^  ([a-z_][a-z0-9_]*):\s*$", line):
                current = m.group(1)
            elif current and (m := re.match(r"^    fail_closed:\s*(\S+)", line)):
                flags[current] = m.group(1).strip().lower() == "true"
        return flags

    @staticmethod
    def _call_site_flags() -> dict[str, bool]:
        flags: dict[str, bool] = {}
        # Ported guards: keyed on the module's OWN RULE_NAME rather than its
        # filename, because the verb name, the module name and the rule name
        # deliberately differ — the port carried RULE_NAME unchanged so old
        # and new routing-log rows stay comparable.
        for module in sorted(WHEEL_HOOKS.glob("*.py")):
            body = module.read_text()
            m = re.search(r"run_hook_result\((?:[^)]|\n)*?fail_closed=(True|False)", body)
            if not m:
                continue
            name = re.search(r'^RULE_NAME\s*=\s*"([^"]+)"', body, re.MULTILINE)
            if name:
                flags[name.group(1)] = m.group(1) == "True"
        # nexus-wauo1.22 (RDR-219 plan-audit residual 2): "`routing/` holds
        # no Python at all" was already stale when this comment was
        # written — `subagent_git_write_requires_orchestrator.py` and
        # `phase_review_close_requires_gate.py` stayed plugin-resident
        # through nexus-t9klx's port (7.58.0 kept hooks.json on the
        # scripts, not the verbs; see test_hooks_json_shape_lint.py's
        # docstring), and RDR-219 deliberately adds a THIRD plugin-
        # resident guard that is never ported at all. Scanning only
        # WHEEL_HOOKS silently reported those rules as having no call
        # site — the same "registry names a rule the scan can't see"
        # failure test_every_registry_rule_has_a_call_site exists to
        # catch, one level up. `_lib.run_hook(` (with the `_lib.` prefix,
        # so `_lib.py`'s own `def run_hook(` never self-matches) is these
        # scripts' real call-site shape, not `run_hook_result(`.
        for module in sorted(ROUTING.glob("*.py")):
            body = module.read_text()
            m = re.search(r"_lib\.run_hook\((?:[^)]|\n)*?fail_closed=(True|False)", body)
            if not m:
                continue
            name = re.search(r'^RULE_NAME\s*=\s*"([^"]+)"', body, re.MULTILINE)
            if name:
                flags.setdefault(name.group(1), m.group(1) == "True")
        return flags

    def test_every_registry_rule_has_a_call_site(self):
        registry = self._registry_flags()
        assert registry, "parsed no rules out of registry.yaml — the parser rotted"
        missing = set(registry) - set(self._call_site_flags())
        assert not missing, f"registry names rules with no run_hook call site: {missing}"

    def test_every_call_site_is_in_the_registry(self):
        call_sites = self._call_site_flags()
        assert call_sites, "found no run_hook call sites — the scan rotted"
        missing = set(call_sites) - set(self._registry_flags())
        assert not missing, f"hooks call run_hook but are not in registry.yaml: {missing}"

    def test_the_flags_match(self):
        registry, call_sites = self._registry_flags(), self._call_site_flags()
        disagree = {
            rule: (registry[rule], call_sites[rule])
            for rule in registry.keys() & call_sites.keys()
            if registry[rule] != call_sites[rule]
        }
        assert not disagree, (
            "registry.yaml and the call site disagree on fail_closed "
            f"(rule: registry, call-site): {disagree}. README item 5 states "
            "the contract as both together."
        )

    def test_phase_review_close_is_still_the_fail_closed_one(self):
        """Named rather than counted. If this rule ever goes fail-open the
        change should have to delete this line and say why."""
        assert self._registry_flags()["phase_review_close_requires_gate"] is True
        assert self._call_site_flags()["phase_review_close_requires_gate"] is True

    @pytest.mark.parametrize(
        "rule", ["subagent_git_write_requires_orchestrator"]
    )
    def test_the_deliberately_fail_open_rules_stay_fail_open(self, rule):
        """registry.yaml carries Hal's 2026-07-25 reasoning for this one: a
        crash in the guard must not brick every agent's Bash, and the real
        open/closed split lives INSIDE the hook. Flipping it to fail-closed
        at the wrapper would bypass that decision."""
        assert self._registry_flags()[rule] is False
        assert self._call_site_flags()[rule] is False
