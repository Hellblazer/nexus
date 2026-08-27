# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""An e2e gate that installs must neutralise the ambient generation root.

nexus-utpuw.18. Sibling of ``tests/test_e2e_gates_isolate_home.py``, and it
exists because $HOME isolation — the property that test enforces — is NOT
sufficient here.

THE HOLE. Under the generation layout an install lands in
``<tools>/gen-<stamp>``, and ``nx_tools_dir`` resolves ``<tools>`` from
``NX_TOOLS_DIR`` when it is set, falling back to ``$HOME/.local/share/nexus/
tools`` only when it is not. The env var WINS. So an operator with
``NX_TOOLS_DIR`` exported in their shell defeats a $HOME-isolated gate
completely: every generation the gate builds lands in their REAL tool root
while every command in the transcript still looks correctly fenced. The gate
reports success and the developer's live install has been written to — the
exact outcome ``release-sandbox.sh``'s own step-1 comment says must never
happen.

This is the same reasoning ``fresh-install-mvv.sh`` already applies to ambient
``UV_TOOL_DIR``/``XDG_*``, which is why it reaches for ``env -i``. The
generation-layout pair was simply never added when the layout landed.

TWO SANCTIONED CURES, and which one is right depends on how the gate isolates:
* CLEAR them (``unset``/``env -i``) when the gate already redirects ``$HOME``.
  The root then resolves under the sandbox home, keeping ONE definition of
  where the sandbox lives. This is what ``sandbox.sh``'s generated ``activate``
  does.
* PIN them to sandbox paths when the gate does NOT globally redirect ``$HOME``
  — ``upgrade-shakeout.sh`` runs the sandbox ``nx`` off ``PATH`` and carries
  ``HOME`` per call, so it has no ``$HOME`` to inherit from.

KNOWN LIMIT, stated because a false NEGATIVE is the direction that hurts here:
``_INSTALLS`` reads each ``.sh`` only. A gate whose install call lives in a
sibling driver — ``gen-flip-live-holder.sh`` delegates to
``lib/gen_flip_holder.py`` — is not classified as installing and is therefore
not checked. That gate happens to run under ``env -i`` AND pin both variables,
so it is neutralised either way; the point is that the sweep would not have
told us. Following one level of sibling reference would close it, at the cost
of a classifier that has to parse shell. If a second delegating gate appears,
close it then.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_E2E = Path(__file__).parent / "e2e"

#: A gate "installs" if it can create an install tree by any route.
_INSTALLS = re.compile(
    r"uv tool install|reinstall-tool\.sh|install_generation\.sh|nx self install"
)

#: Neutralising the ambient override: scrub everything, clear the pair, or pin
#: it. A sourced ``activate`` counts — ``sandbox.sh`` generates it and its own
#: test below pins what it must contain.
_NEUTRALISED = (
    re.compile(r"\benv\s+-i\b"),
    re.compile(r"^\s*unset\b[^\n]*\bNX_TOOLS_DIR\b", re.M),
    re.compile(r"NX_TOOLS_DIR\b[^\n]*NX_BIN_DIR|unset[^\n]*NX_TOOLS_DIR"),
    re.compile(r"^\s*export\s+NX_TOOLS_DIR=", re.M),
    re.compile(r"^\s*\.\s+\S*activate\b", re.M),
    re.compile(r"^\s*source\s+\S*activate\b", re.M),
)


def _code_only(body: str) -> str:
    """*body* with whole-line shell comments removed.

    EVERY pattern in this module is matched against this, not against the raw
    file, and that is load-bearing rather than tidy. Found by RG-E's
    test-validator (nexus-utpuw.25): removing upgrade-shakeout.sh's
    NX_TOOLS_DIR/NX_BIN_DIR pin left this suite GREEN, because that file
    contains the substring ``env -i`` in a PROSE COMMENT — one written by
    nexus-utpuw.18 itself, explaining why a SIBLING gate reaches for `env -i`.
    The guard was satisfied by documentation describing a mechanism the file
    does not use.

    It cuts both ways, so both directions are stripped: a comment mentioning
    ``uv tool install`` would also have classified a non-installing gate as
    installing, and then "neutralised" it on the strength of more prose.

    RESIDUAL LIMIT, stated rather than left to be discovered: this drops
    whole-line comments only. A TRAILING comment (``code  # env -i``) still
    matches. Stripping those needs real shell lexing — ``#`` inside a string
    is not a comment — and buying that with a hand-rolled regex would trade a
    known narrow hole for an unknown wide one.
    """
    return "\n".join(
        line for line in body.splitlines() if not line.lstrip().startswith("#")
    )


def _gates() -> list[Path]:
    return sorted(p for p in _E2E.glob("*.sh") if p.is_file())


def _installing_gates() -> list[Path]:
    return [p for p in _gates() if _INSTALLS.search(_code_only(p.read_text()))]


@pytest.mark.lint
def test_the_sweep_finds_installing_gates() -> None:
    """Non-vacuity. Every assertion below is quantified over "gates that
    install"; if that set ever empties — a rename, a moved directory, a
    tightened regex — the parametrized test still reports green over nothing.
    The floor is deliberately low: this asserts the instrument works, not how
    many gates happen to exist."""
    found = _installing_gates()
    assert len(found) >= 3, (
        f"only {len(found)} installing e2e gate(s) found ({[p.name for p in found]}) "
        "— the sweep has stopped seeing its subjects, so the checks below prove "
        "nothing"
    )


@pytest.mark.lint
@pytest.mark.parametrize("gate", _installing_gates(), ids=lambda p: p.name)
def test_an_installing_gate_neutralises_the_ambient_generation_root(gate: Path) -> None:
    body = _code_only(gate.read_text())
    if any(pattern.search(body) for pattern in _NEUTRALISED):
        return
    pytest.fail(
        f"{gate.name} can create an install tree but neither clears nor pins "
        "NX_TOOLS_DIR/NX_BIN_DIR. An operator with NX_TOOLS_DIR exported would "
        "have this gate write generations into their REAL tool root while every "
        "command still looked fenced by $HOME. Clear them (unset / env -i) if "
        "the gate redirects $HOME; pin them to sandbox paths if it does not. "
        "See this module's docstring.",
        pytrace=False,
    )


@pytest.mark.lint
def test_the_generated_activate_clears_the_generation_root() -> None:
    """``sandbox.sh`` writes the ``activate`` that ``release-sandbox.sh``
    sources, so the sourced-activate exemption above is only honest while that
    file actually clears the pair. This is what makes it honest."""
    # Through _code_only like everything else in this module. It was NOT, and
    # the docstring above claimed it was — the same comment-vulnerability class
    # this module exists to close, left dormant twenty lines from the fix for
    # it (RG-E reviewer 2, nexus-utpuw.25). Dormant rather than live, since
    # sandbox.sh's unset really is code; the claim was the defect.
    body = _code_only((_E2E / "sandbox.sh").read_text())
    unset_lines = [ln for ln in body.splitlines() if "unset" in ln and "NX_SESSION_ID" in ln]
    assert unset_lines, "sandbox.sh no longer writes the targeted-unset line"
    joined = "\n".join(unset_lines)
    for name in ("NX_TOOLS_DIR", "NX_BIN_DIR"):
        assert name in joined, (
            f"sandbox.sh's generated activate no longer unsets {name}. "
            "release-sandbox.sh isolates by $HOME alone and every gate that "
            "sources this activate inherits the gap: an exported "
            f"{name} would redirect the sandbox's generations into the "
            "developer's live install."
        )
