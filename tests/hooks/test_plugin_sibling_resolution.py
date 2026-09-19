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
"""
from __future__ import annotations

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
    ("nexus.hooks.stop_verification", "_plugin_root"),
    ("nexus.hooks.session_context", "_plugin_root"),
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


def _modules_reaching_the_plugin() -> set[str]:
    """Every hooks module whose source RESOLVES a path into the plugin."""
    found = set()
    for path in HOOKS_SRC.glob("*.py"):
        text = path.read_text()
        if any(rx.search(text) for rx in _RESOLUTION_SHAPES):
            found.add(f"nexus.hooks.{path.stem}")
    return found


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
        # Env-only by design, no fallback: the bash it ports had no `:-`
        # default either, and its own docstring carries the reasoning.
        "nexus.hooks.subagent_start",
        # Resolves a candidate LIST (env, then checkout) and returns the
        # first that exists, rather than a single path; covered by
        # tests/hooks/test_tuple_projection_module.py.
        "nexus.hooks.tuple_projection",
        # Reaches read_verification_config.py through stop_verification's
        # _plugin_root, which this file does pin.
        "nexus.hooks.pre_close_verification",
    }
    surprises = unlisted - known_ok
    assert not surprises, (
        f"hooks modules reach the plugin but are neither in RESOLVERS nor "
        f"acknowledged: {sorted(surprises)}"
    )
