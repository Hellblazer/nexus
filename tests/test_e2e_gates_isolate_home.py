# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""An e2e gate must never invoke `nx` against the operator's real HOME.

nexus-pfuns follow-up #2. `nx --version` is not a read: `cli.py` stamps
`last_seen_version` on invocation. So a single unfenced `nx` call inside a gate
writes the operator's `~/.config/nexus` — and because `nexus_config_dir()` falls
back to `Path.home()/".config"/"nexus"`, pinning NEXUS_CONFIG_DIR does not stop
it either.

MEASURED, 2026-08-24. `upgrade-shakeout.sh` exported PATH to its sandbox tool
dir but never exported HOME, and four `nx --version` calls carried no
`HOME="$SANDBOX"` prefix. They ran the SANDBOX's nx (FROM_VERSION = latest
stable = 7.16.3) against the REAL home, stamping
`~/.config/nexus/last_seen_version` = 7.16.3 mid-release-battery. A
concurrently-running `local-service-gate` leg saw the mutation through the
pfuns guard and reported exit 1 over a run in which all 560 tests passed. The
stamped value is what identifies the writer: the tree under test was 7.17.0, so
only the sandbox binary could have written 7.16.3.

Each gate may isolate however it likes — a sourced activate, an `env -i`
wrapper, a global export, `fence_home`, or a per-call prefix — but it must
isolate SOMEHOW, and this test is what says so.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_E2E = Path(__file__).parent / "e2e"

#: Sanctioned whole-script isolation mechanisms. A script using any of these
#: has HOME redirected for every command, so per-call prefixes are unnecessary.
_GLOBAL_ISOLATION = (
    re.compile(r"^\s*export\s+HOME=", re.M),          # global export
    re.compile(r"^\s*\.\s+\S*activate\b", re.M),      # sourced sandbox activate
    re.compile(r"^\s*source\s+\S*activate\b", re.M),
    re.compile(r"\benv\s+-i\b"),                      # scrubbed-env wrapper
    re.compile(r"\bfence_home\b"),                    # the nexus-pfuns mirror
)

#: A command-position `nx` invocation. Anchored to line start or a shell
#: operator so `$(nx ...)`, `| nx`, `&& nx` all count, while the word "nx"
#: inside prose, an echo string, or a flag name does not.
_NX_CALL = re.compile(r"(?:^|[;&|(]|\$\()\s*(?:[A-Z_]+=\S+\s+)*nx\s+[a-z-]")

#: An invocation is fenced when a HOME= assignment (or env -i) precedes `nx`
#: on the same command.
_FENCED_CALL = re.compile(r"(?:HOME=\S+|env\s+-i)[^\n]*?\bnx\s+[a-z-]")


def _gate_scripts() -> list[Path]:
    return sorted(p for p in _E2E.glob("*.sh") if p.is_file())


def _strip_noise(line: str) -> str:
    """Drop comments and the obvious string contexts that mention nx in prose."""
    line = re.sub(r"#.*$", "", line)
    line = re.sub(r"_(?:pass|die|step|assert_fail|check_no_demoted_verb)\b.*$", "", line)
    line = re.sub(r"\becho\s.*$", "", line)
    return line


@pytest.mark.parametrize("script", _gate_scripts(), ids=lambda p: p.name)
def test_every_nx_call_in_a_gate_is_home_isolated(script: Path) -> None:
    body = script.read_text()
    if any(rx.search(body) for rx in _GLOBAL_ISOLATION):
        return  # whole-script isolation; per-call prefixes not required

    offenders: list[str] = []
    for n, raw in enumerate(body.split("\n"), 1):
        line = _strip_noise(raw)
        if not _NX_CALL.search(line):
            continue
        if _FENCED_CALL.search(line):
            continue
        offenders.append(f"  {script.name}:{n}: {raw.strip()[:100]}")

    assert not offenders, (
        f"{script.name} invokes nx without HOME isolation. `nx --version` is a "
        "WRITE — cli.py stamps last_seen_version on invocation — so these run "
        "against the operator's real ~/.config/nexus:\n"
        + "\n".join(offenders)
        + "\n\nFix: prefix with HOME=\"$SANDBOX\", or adopt a whole-script "
        "mechanism (export HOME, source an activate, env -i, fence_home)."
    )


def test_the_lint_can_actually_fail() -> None:
    """Positive control. Every assertion above passes vacuously if the
    command-position regex matches nothing, so prove it matches the exact shape
    that caused the incident and not the prose that surrounds it."""
    assert _NX_CALL.search('OLD_VERSION_LINE="$(nx --version)"')
    assert _NX_CALL.search("nx daemon service stop")
    assert not _FENCED_CALL.search('OLD_VERSION_LINE="$(nx --version)"')
    assert _FENCED_CALL.search('X="$(HOME="$SANDBOX" nx --version)"')
    # prose and labels must NOT register
    assert not _NX_CALL.search(_strip_noise('_pass "nx doctor names no demoted verb"'))
    assert not _NX_CALL.search(_strip_noise('# run nx upgrade to fix it'))


def test_the_incident_line_would_be_caught_today() -> None:
    """The exact 2026-08-24 line, verbatim, against the real predicate."""
    incident = '[[ "$(nx --version)" == *"$FROM_VERSION"* ]] || _die "version mismatch"'
    line = _strip_noise(incident)
    assert _NX_CALL.search(line) and not _FENCED_CALL.search(line), (
        "the line that stamped the operator's config dir would slip through"
    )
