# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every ported hook that reaches a plugin script honours CLAUDE_PLUGIN_ROOT.

RDR-215 created a dependency direction that did not exist before: a hook
in the WHEEL reaching a script in the PLUGIN. The bash never had this
problem -- ``dirname "${BASH_SOURCE[0]}"`` was always right, because the
caller was the sibling.

A ported module is not the sibling. At ``<repo>/src/nexus/hooks/x.py`` a
checkout-relative anchor finds ``<repo>/conexus``; installed, at
``<...>/site-packages/nexus/hooks/x.py``, the same anchor lands in the
interpreter's lib directory. Every caller here treats a missing plugin
script as a no-op, so getting it wrong is SILENT -- the hook reports
nothing wrong, forever, on every box that is not a checkout.

That shipped once (``divergence_language_guard``, nexus-q02nx.21
critique). Three sibling modules had it right and one did not, and
nothing compared them, so this is a gate over the CLASS rather than a
regression test for the one instance.

**THIS FILE IS MEANT TO SHRINK TO NOTHING.** Honouring the variable is
the best a module can do while it still reaches the plugin; not reaching
it at all is better, and is what RDR-215 is for. So an entry LEAVING
``RESOLVERS`` because its module was ported into the wheel is a win, not
a gap -- ``stop_verification`` left that way at bead nexus-b5ugt, which
moved ``read_verification_config.py`` into ``nexus.hooks.verification_
config``. When the list empties, delete the file and say why.

That distinction matters because the two look identical from here: a
module that stopped reaching the plugin and a module that stopped being
scanned both just disappear from the parametrization.
``test_the_resolver_list_covers_what_reaches_the_plugin`` is what tells
them apart -- it rescans the package, so a module that still reaches the
plugin cannot leave quietly.
"""
from __future__ import annotations

import ast
import importlib
import os
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOKS_SRC = REPO_ROOT / "src" / "nexus" / "hooks"

#: (module, callable returning a path into the plugin).
#: ``subagent_start`` resolves off the env var ALONE by design -- no
#: fallback, because the bash it ports had none -- which satisfies this
#: gate for the same reason the others do.
RESOLVERS: list[tuple[str, str]] = [
    ("nexus.hooks.divergence_language_guard", "_scan_script"),
    # stop_verification was here until bead nexus-b5ugt ported
    # read_verification_config.py into the wheel. It no longer reaches
    # the plugin, so there is nothing left for this gate to check.
    # session_context left at bead nexus-b5ugt too: it was the SECOND
    # caller of the plugin's t2_prefix_scan.py, missed when the first was
    # ported, and porting it emptied its _plugin_root of callers.
    ("nexus.hooks._plugin", "plugin_root"),
]


#: Source shapes that RESOLVE a plugin path, as opposed to naming one in
#: prose. Nearly every ported module's docstring says "Port of
#: conexus/hooks/scripts/X" -- that is provenance, not a dependency, and
#: matching on it would put nine modules in this gate that never touch the
#: filesystem.
_RESOLUTION_SHAPES = (
    re.compile(r"\bplugin_root\s*\("),
    re.compile(r"\bplugin_script\s*\("),
    re.compile(r"CLAUDE_PLUGIN_ROOT"),
    re.compile(r'parents\[\d+\][^\n]*"conexus"'),
)


def _code_only(source: str) -> str:
    """*source* with docstrings and comments removed.

    The scan below asks whether a module RESOLVES a plugin path. Prose
    that merely names ``CLAUDE_PLUGIN_ROOT`` is not that, and after bead
    nexus-b5ugt it is common: four modules were ported into the wheel
    precisely so they would stop reaching the plugin, and each one's
    docstring explains the defect it fixes by naming the variable.

    Scanning raw text put all four into ``known_ok``, which is the wrong
    place for them -- ``known_ok`` means "reaches the plugin, and that is
    accepted", and these do not reach it at all. Left that way the
    exemption list grows every time a module is fixed, until the gate
    exempts everything it was built to check. Same failure the verdict
    scanner in tests/test_deciding_hooks_are_command_tier.py has: a
    substring match cannot tell an identifier from a sentence.
    """
    tree = ast.parse(source)
    spans: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str) and node.end_lineno is not None:
                spans.append((node.lineno, node.end_lineno))
    dropped = {n for start, end in spans for n in range(start, end + 1)}
    lines = [
        "" if i in dropped else line.split("#", 1)[0]
        for i, line in enumerate(source.splitlines(), start=1)
    ]
    return "\n".join(lines)


def _modules_reaching_the_plugin() -> set[str]:
    """Every hooks module whose CODE resolves a path into the plugin."""
    found = set()
    for path in HOOKS_SRC.glob("*.py"):
        text = _code_only(path.read_text())
        if any(rx.search(text) for rx in _RESOLUTION_SHAPES):
            found.add(f"nexus.hooks.{path.stem}")
    return found


def test_the_scan_reads_code_not_prose() -> None:
    """Non-vacuity for :func:`_code_only`, and the reason it exists.

    A module whose only mention of the variable is in its docstring must
    not register as reaching the plugin. Asserted against a real module
    rather than a synthetic string, because the synthetic version would
    be written with the same assumption the scan makes.
    """
    ported = HOOKS_SRC / "verification_config.py"
    assert ported.is_file(), "the ported config reader is gone; rewrite this test"
    raw = ported.read_text()
    assert "CLAUDE_PLUGIN_ROOT" in raw, (
        "verification_config no longer names the variable even in prose, so "
        "this guard cannot detect a prose-only match any more"
    )
    assert "CLAUDE_PLUGIN_ROOT" not in _code_only(raw), (
        "the code-only view still contains the variable, so either the module "
        "really does resolve a plugin path now, or _code_only stopped working"
    )
    assert "nexus.hooks.verification_config" not in _modules_reaching_the_plugin()


@pytest.mark.parametrize("module_name,attr", RESOLVERS, ids=[m for m, _ in RESOLVERS])
def test_the_resolver_follows_claude_plugin_root(
    module_name: str, attr: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the variable set, resolution must not depend on ``__file__``.

    This is the whole defect: a resolver that ignores it works in a
    checkout and silently resolves nowhere once installed.
    """
    stub = tmp_path / "plugin-root"
    (stub / "hooks" / "scripts").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(stub))
    mod = importlib.import_module(module_name)
    got = Path(getattr(mod, attr)())
    assert str(got).startswith(str(stub)), (
        f"{module_name}.{attr}() returned {got}, which is not under "
        f"CLAUDE_PLUGIN_ROOT={stub}. Installed, this module is in "
        f"site-packages and a checkout-relative anchor resolves to the "
        f"interpreter's lib directory -- the plugin script is never found "
        f"and the hook no-ops in silence."
    )


def test_no_hooks_module_anchors_on_the_checkout_alone() -> None:
    """A ``parents[N]`` walk to the plugin, with no env branch, is the bug.

    Source-level rather than behavioural because the failure only appears
    in an installed layout, which a test in a checkout cannot produce
    without staging a fake site-packages. The two are complementary: the
    test above proves the env var is honoured where it is set, this one
    proves nobody has re-introduced an anchor that ignores it.
    """
    offenders = []
    for path in sorted(HOOKS_SRC.glob("*.py")):
        text = path.read_text()
        # Across lines, not within one: the first draft of this test used
        # [^\n]* and missed the two-line spelling, which is the form the
        # defect actually shipped in --
        #     repo = Path(__file__).resolve().parents[3]
        #     return repo / "conexus" / ...
        # The behavioural test above caught it and this one did not, which
        # is the same class of blindness both are here to prevent.
        for m in re.finditer(r'parents\[\d+\](?:.(?!\ndef ))*?"conexus"', text, re.S):
            line = text[: m.start()].count("\n") + 1
            # _plugin.py is where the fallback is supposed to live.
            if path.name == "_plugin.py":
                continue
            offenders.append(f"{path.name}:{line}")
    assert not offenders, (
        "these walk to the plugin off __file__ instead of calling "
        f"nexus.hooks._plugin.plugin_root(): {offenders}"
    )


def test_the_resolver_list_covers_what_reaches_the_plugin() -> None:
    """The parametrized list must not fall behind the package.

    A new ported hook that reaches a plugin script and is not listed here
    is exactly the case this file exists for, and it would join silently.
    """
    listed = {m for m, _ in RESOLVERS}
    reaching = _modules_reaching_the_plugin()
    assert reaching, "no hooks module mentions the plugin; the scan went blind"
    unlisted = reaching - listed
    known_ok = {
        # Genuinely reaches the plugin, and correctly: it resolves the
        # routing/ guards, which are still plugin-resident and out of
        # scope for bead nexus-b5ugt. It honours the env var (with the
        # unexpanded-placeholder guard) and falls back to the checkout,
        # but through its own inline candidate list rather than a single
        # accessor, so there is no callable for RESOLVERS to pin.
        #
        # This set held six entries mid-merge, five of them modules that
        # had just been ported into the wheel and named CLAUDE_PLUGIN_ROOT
        # only in a docstring explaining the defect they fixed. Exempting
        # those was backwards -- known_ok means "reaches the plugin, and
        # that is accepted" -- and would have grown the list every time a
        # module was fixed. _code_only fixed the scan instead, and five of
        # the six stopped matching.
        "nexus.hooks.pre_close_verification",
        # Reaches the plugin because reaching the plugin IS its job
        # (nexus-t9klx): it reads the INSTALLED plugin's
        # .claude-plugin/plugin.json version to detect plugin<->CLI skew,
        # which is the whole of RDR-143. There is no sibling SCRIPT being
        # resolved here — the thing it reads is the plugin's own manifest,
        # and it would still read it if every script were gone.
        "nexus.hooks.version_lockstep",
    }
    surprises = unlisted - known_ok
    assert not surprises, (
        f"hooks modules reach the plugin but are neither in RESOLVERS nor "
        f"acknowledged: {sorted(surprises)}"
    )
