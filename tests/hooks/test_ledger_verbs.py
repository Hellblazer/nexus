# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The ledger's operator verbs on ``nx-hook`` (RDR-215 bead nexus-q02nx.14).

These are the repoint target for every consumer that used to
``source tests/e2e/lib/expectations.sh``, so they are driven HERE THE WAY
THOSE CONSUMERS DRIVE THEM -- a real ``nx-hook`` subprocess, arguments on
argv, no stdin redirection, and the exit code read as the answer. A test
that called ``run()`` in-process would have missed the defect that made
this file necessary.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from nexus._hook_runtime.entry import LEDGER_VERBS, VERB_TABLE
from nexus.hooks.ledger_verbs import VERBS
from nexus.mcp.hooks import HOOK_TOOLS

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / "tests" / "e2e" / "lib" / "expectations.sh"

#: Bounds a HANG, not performance. The defect this file pins made the verb
#: block forever, so every invocation here is time-bounded and a timeout is
#: a failure rather than a slow pass.
_BUDGET = 60


def _nx_hook(*args: str, state: Path, stdin=subprocess.DEVNULL, env_extra: dict | None = None):
    """Run the real console script, as a consumer script would."""
    env = {**os.environ, "XDG_STATE_HOME": str(state), **(env_extra or {})}
    return subprocess.run(
        [sys.executable, "-c",
         "from nexus._hook_runtime.entry import main; main()", *args],
        capture_output=True, text=True, timeout=_BUDGET, env=env, stdin=stdin,
    )


def _bash(verb: str, *args: str, state: Path, env_extra: dict | None = None):
    env = {**os.environ, "XDG_STATE_HOME": str(state), **(env_extra or {})}
    quoted = " ".join(f"'{a}'" for a in args)
    return subprocess.run(
        ["bash", "-c", f'source "{LIB}"; {verb} {quoted}'],
        capture_output=True, text=True, timeout=_BUDGET, env=env,
    )


@pytest.fixture()
def ledger(tmp_path: Path) -> Path:
    """A state dir holding one session with a real, mixed ledger."""
    d = tmp_path / "nexus" / "orchestration"
    d.mkdir(parents=True)
    (d / "sess-probe.expectations").write_text(
        "2026-09-19T00:00:00Z\tEXPECT\tconexus:developer\tbackground\td1\n"
        "2026-09-19T00:00:01Z\tSTART\ta1\tconexus:developer\n"
        "2026-09-19T00:00:02Z\tSTART\ta2\tconexus:critic\n"
    )
    return tmp_path


#: A PATH with no `nx` on it at all -- real system dirs only, so `bash`
#: itself (needed to run the library twin) still resolves. Used for the
#: SPACE_FALLBACK/VERIFY_FALLBACK "PATH has no nx" branch: this repo's dev
#: box always has a real `nx` on the ambient PATH, so that branch is only
#: reachable by actually scrubbing it, not by hoping the box lacks one.
_NO_NX_PATH = "/usr/bin:/bin"

#: A fake `nx` whose three subcommands (`tuple list`, `tuple templates`,
#: `tuple rd`) answer from env vars, so a differential test can drive both
#: the Python port and the bash original through the exact same responses
#: without depending on this shared dev box's live, ever-changing tuple
#: space. Args are matched by their space-joined form ($*), which is
#: stable for the three call shapes `_census_space`/`_census_verify_absent`
#: actually make.
#: A default assigned via a plain variable, never inlined into a
#: ``${VAR:-word}`` expansion: bash's parameter-expansion parser closes
#: ``${...}`` at the FIRST unescaped ``}`` in ``word`` -- it does not
#: brace-match a literal ``{`` the way it looks like it should -- so a
#: JSON-object default embedded directly (``${V:-{"a":1}}``) truncates and
#: leaks its own trailing ``}`` as literal output once ``V`` is ever SET.
#: Measured live while writing this fixture (`printf '[%s]' "${X:-a{b}c}"`
#: prints ``[a{bc}]`` unset, ``[SETc}]`` set) -- exactly the corruption a
#: differential test here must not itself introduce.
_FAKE_NX = """#!/bin/bash
templates_default='{"templates":[]}'
case "$*" in
  "tuple list --prefix ledger/ --json")
    printf '%s' "${FAKE_NX_LIST_JSON:-[]}"
    exit "${FAKE_NX_LIST_RC:-0}"
    ;;
  "tuple templates --json")
    printf '%s' "${FAKE_NX_TEMPLATES_JSON:-$templates_default}"
    exit "${FAKE_NX_TEMPLATES_RC:-0}"
    ;;
  tuple\\ rd\\ *)
    printf '%s' "${FAKE_NX_RD_JSON:-[]}"
    exit "${FAKE_NX_RD_RC:-0}"
    ;;
  *)
    exit 1
    ;;
esac
"""


@pytest.fixture()
def fake_nx_path(tmp_path: Path) -> str:
    """A PATH with a controllable fake `nx` shadowing the real one.

    Prepending its dir puts it first in PATH resolution for BOTH the
    python port (`shutil.which`/`subprocess.run(["nx", ...])`) and the
    bash original (`command -v nx` / a bare `nx` call) -- the same
    substitution, seen identically by both sides.
    """
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    script = bindir / "nx"
    script.write_text(_FAKE_NX)
    script.chmod(0o755)
    return f"{bindir}:{_NO_NX_PATH}"


class TestItDoesNotReadStdin:
    """The defect that made this file necessary, pinned.

    ``main()`` read stdin for every verb. ``read_payload`` guards a TTY,
    but a script that invokes ``nx-hook`` without redirecting hands it an
    inherited pipe with no writer, so ``read()`` blocks on an EOF that
    never arrives. Measured before the fix: the command hung until killed.
    Every class-1 consumer this bead repoints would have hung identically,
    and a test that passed a payload -- or ``DEVNULL`` -- would have seen
    nothing wrong.
    """

    def test_an_open_empty_stdin_pipe_does_not_hang(self, ledger):
        """stdin=PIPE with nothing written and the handle left OPEN is the
        exact shape a consumer script produces. Kept deliberately distinct
        from DEVNULL, which closes immediately and cannot reproduce it."""
        env = {**os.environ, "XDG_STATE_HOME": str(ledger)}
        proc = subprocess.Popen(
            [sys.executable, "-c",
             "from nexus._hook_runtime.entry import main; main()",
             "expectations_undeclared", "sess-probe"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env,
        )
        try:
            out, _ = proc.communicate(timeout=_BUDGET)
        except subprocess.TimeoutExpired:
            proc.kill()
            pytest.fail(
                "nx-hook blocked on an unredirected stdin — the ledger verbs "
                "take arguments, not a payload, and must never read it"
            )
        assert proc.returncode == 2, out
        assert "UNDECLARED\ta2" in out

    @pytest.mark.parametrize(
        "verb", ["expectations_census", "expectations_undeclared"]
    )
    def test_every_read_verb_survives_it(self, ledger, verb):
        env = {**os.environ, "XDG_STATE_HOME": str(ledger)}
        proc = subprocess.Popen(
            [sys.executable, "-c",
             "from nexus._hook_runtime.entry import main; main()",
             verb, "sess-probe"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env,
        )
        try:
            proc.communicate(timeout=_BUDGET)
        except subprocess.TimeoutExpired:
            proc.kill()
            pytest.fail(f"{verb} blocked on an unredirected stdin")


class TestTheyAgreeWithTheLibraryTheyReplace:
    """STDOUT AND EXIT CODE BOTH, because callers use both: the scripts
    pipe and grep the lines, and branch on the code."""

    @pytest.mark.parametrize(
        "verb", ["expectations_undeclared", "expectations_census"]
    )
    def test_a_populated_session_matches(self, ledger, verb):
        mine = _nx_hook(verb, "sess-probe", state=ledger)
        theirs = _bash(verb, "sess-probe", state=ledger)
        assert mine.stdout == theirs.stdout, verb
        assert mine.returncode == theirs.returncode, verb

    @pytest.mark.parametrize(
        "verb", ["expectations_undeclared", "expectations_census"]
    )
    def test_an_absent_session_matches(self, ledger, verb):
        """rc=3 for undeclared: no ledger file is not evidence of
        cleanliness (nexus-ahl9v), and that distinction is the whole
        reason these verbs are driven by exit code."""
        mine = _nx_hook(verb, "no-such-session", state=ledger)
        theirs = _bash(verb, "no-such-session", state=ledger)
        assert mine.returncode == theirs.returncode, verb


class TestTheSpaceAndVerifyLines:
    """The RDR-205 tuple-space cross-check lines (``SPACE_*``/``VERIFY_*``,
    nexus-em75s.19) that ``expectations_census`` appends after its TSV
    output -- the gap this bead exists to close.

    Driven through a FAKE ``nx`` (see ``fake_nx_path``/``_NO_NX_PATH``)
    rather than the real one ``TestTheyAgreeWithTheLibraryTheyReplace``
    above uses: this repo's dev box has a real, LIVE, shared tuple space
    (other sessions' ledgers), so the real ``nx tuple list --prefix
    ledger/`` result is non-deterministic across runs and the specific
    branch it hits (BLINDSPOT vs. NEVER_RAN vs. OUTSIDE_WINDOW) depends on
    ambient state this test does not control. Each case below constructs
    its own deterministic response so the exact branch, and the exact
    reason string, is pinned rather than incidental.

    Every case compares ``mine``/``theirs`` byte for byte, same bar as the
    class above -- both sides see the identical fake ``nx``, so a
    mismatch here is a real divergence, not fixture noise.

    NOT covered by a real fixture here, and said so rather than mocked: a
    genuine 124 (deadline-exceeded) bounded-call timeout. Reaching it
    needs a fake ``nx`` that sleeps past ``NX_EXPECT_CENSUS_NX_TIMEOUT_S``,
    which would tax every run of this file by that many seconds for a
    branch whose bash and python sides already share the same fallback
    message shape (``nx tuple ... failed (rc=%d): %s``) that every
    non-timeout non-zero exit here already exercises byte-for-byte. Also
    not covered: ``nx tuple rd``'s own failure/unparseable-JSON paths --
    reaching them needs ``FAKE_NX_TEMPLATES_JSON`` to declare ``verify``
    (so ``_census_verify_absent`` proceeds past ``VERIFY_UNVERIFIABLE`` to
    the ``rd`` call at all), which is exercised below in
    ``test_a_populated_matching_subspace_and_declared_verify``.
    """

    def test_nx_absent_from_path(self, ledger):
        env = {"PATH": _NO_NX_PATH}
        mine = _nx_hook("expectations_census", "sess-probe", state=ledger, env_extra=env)
        theirs = _bash("expectations_census", "sess-probe", state=ledger, env_extra=env)
        assert mine.stdout == theirs.stdout
        assert mine.returncode == theirs.returncode
        assert "SPACE_FALLBACK\treason=PATH has no nx" in mine.stdout
        assert "VERIFY_FALLBACK\treason=PATH has no nx" in mine.stdout

    def test_unparseable_json_from_tuple_list(self, ledger, fake_nx_path):
        env = {"PATH": fake_nx_path, "FAKE_NX_LIST_JSON": "not json"}
        mine = _nx_hook("expectations_census", "sess-probe", state=ledger, env_extra=env)
        theirs = _bash("expectations_census", "sess-probe", state=ledger, env_extra=env)
        assert mine.stdout == theirs.stdout
        assert mine.returncode == theirs.returncode
        assert "SPACE_FALLBACK\treason=unparseable JSON from nx tuple list" in mine.stdout
        # Default FAKE_NX_TEMPLATES_JSON (`{"templates":[]}`) declares no
        # `verify` dimension, so the verify half takes its own fallback --
        # exercised as its own branch by the templates-side tests below.
        assert "VERIFY_UNVERIFIABLE\treason=" in mine.stdout

    def test_tuple_list_non_json_array(self, ledger, fake_nx_path):
        """A syntactically valid JSON value that is not an array -- the
        distinct "did not return an array" reason, never folded into the
        parse-failure one above."""
        env = {"PATH": fake_nx_path, "FAKE_NX_LIST_JSON": '{"not": "an array"}'}
        mine = _nx_hook("expectations_census", "sess-probe", state=ledger, env_extra=env)
        theirs = _bash("expectations_census", "sess-probe", state=ledger, env_extra=env)
        assert mine.stdout == theirs.stdout
        assert mine.returncode == theirs.returncode
        assert "SPACE_FALLBACK\treason=nx tuple list --json did not return an array" in mine.stdout

    def test_tuple_list_nonzero_exit(self, ledger, fake_nx_path):
        env = {
            "PATH": fake_nx_path,
            "FAKE_NX_LIST_RC": "3",
            "FAKE_NX_LIST_JSON": "engine unreachable\nretrying...",
        }
        mine = _nx_hook("expectations_census", "sess-probe", state=ledger, env_extra=env)
        theirs = _bash("expectations_census", "sess-probe", state=ledger, env_extra=env)
        assert mine.stdout == theirs.stdout
        assert mine.returncode == theirs.returncode
        # The multi-line reason must fold to ONE line (tab/newline scrubbed),
        # matching bash's `tr '\n\t' '  ' | tr -s ' '`.
        assert (
            "SPACE_FALLBACK\treason=nx tuple list --prefix ledger/ failed "
            "(rc=3): engine unreachable retrying..." in mine.stdout
        )

    def test_a_populated_matching_subspace(self, ledger, fake_nx_path):
        """A non-empty subspace list that DOES contain this session's
        target: SPACE_PRESENT + SPACE_AGE, with a fully deterministic
        drift computed from two fixed ISO-8601 strings (no wall-clock
        component, unlike the NEVER_RAN/OUTSIDE_WINDOW age_seconds field)."""
        env = {
            "PATH": fake_nx_path,
            "FAKE_NX_LIST_JSON": (
                '[{"subspace": "ledger/sess-probe", "total": 5, '
                '"newest_created_at": "2026-09-19T00:00:10Z"}]'
            ),
        }
        mine = _nx_hook("expectations_census", "sess-probe", state=ledger, env_extra=env)
        theirs = _bash("expectations_census", "sess-probe", state=ledger, env_extra=env)
        assert mine.stdout == theirs.stdout
        assert mine.returncode == theirs.returncode
        assert "SPACE_PRESENT\tsubspace=ledger/sess-probe total=5" in mine.stdout
        # tsv_newest is the ledger fixture's last row's timestamp
        # (2026-09-19T00:00:02Z); drift = tsv_newest - space_newest = -8s.
        assert (
            "SPACE_AGE\tspace_newest=2026-09-19T00:00:10Z "
            "tsv_newest=2026-09-19T00:00:02Z drift_seconds=-8" in mine.stdout
        )

    def test_a_populated_matching_subspace_and_declared_verify(self, ledger, fake_nx_path):
        """`verify` declared, and `nx tuple rd` returns rows -- exercises
        the VERIFY_ABSENT_COUNT path all the way through, distinct from
        every case above where templates default to not declaring it."""
        env = {
            "PATH": fake_nx_path,
            "FAKE_NX_LIST_JSON": (
                '[{"subspace": "ledger/sess-probe", "total": 2, '
                '"newest_created_at": "2026-09-19T00:00:02Z"}]'
            ),
            "FAKE_NX_TEMPLATES_JSON": (
                '{"templates": [{"name": "ledger/<session_id>", '
                '"dimensions": {"verify": {"type": "string"}}}]}'
            ),
            "FAKE_NX_RD_JSON": (
                '[{"dims": {"verify": "present"}}, '
                '{"dims": {"verify": "absent"}}, '
                '{"dims": {}}]'
            ),
        }
        mine = _nx_hook("expectations_census", "sess-probe", state=ledger, env_extra=env)
        theirs = _bash("expectations_census", "sess-probe", state=ledger, env_extra=env)
        assert mine.stdout == theirs.stdout
        assert mine.returncode == theirs.returncode
        # One row explicitly "present", two rows counted as absent (one
        # explicit "absent", one with the dimension missing entirely).
        assert "VERIFY_ABSENT_COUNT\tn=2" in mine.stdout


class TestTheWriteVerb:
    def test_it_appends_an_expect_row(self, tmp_path):
        r = _nx_hook(
            "expectations_expect", "sess-w", "conexus:developer",
            "background", "d9", state=tmp_path,
        )
        assert r.returncode == 0, r.stderr
        rows = (
            tmp_path / "nexus" / "orchestration" / "sess-w.expectations"
        ).read_text()
        assert "\tEXPECT\tconexus:developer\tbackground\td9\n" in rows

    def test_the_mode_defaults_to_background(self, tmp_path):
        """Background is the mode that can later require a report, so it
        is the safe default for a hand-declared dispatch."""
        _nx_hook("expectations_expect", "sess-w", "conexus:developer", state=tmp_path)
        rows = (
            tmp_path / "nexus" / "orchestration" / "sess-w.expectations"
        ).read_text()
        assert "\tbackground\t" in rows or rows.rstrip().endswith("background")

    def test_a_refused_type_exits_two_and_writes_nothing(self, tmp_path):
        r = _nx_hook(
            "expectations_expect", "sess-w", "bad type with spaces", state=tmp_path
        )
        assert r.returncode == 2
        assert not (tmp_path / "nexus" / "orchestration" / "sess-w.expectations").exists()


class TestArgumentHandling:
    @pytest.mark.parametrize(
        "verb",
        ["expectations_census", "expectations_undeclared",
         "expectations_reconcile", "expectations_expect"],
    )
    def test_a_missing_session_id_is_a_named_usage_line_not_a_traceback(
        self, verb, tmp_path
    ):
        """Exit 2 with a usage line. NOT 0 -- a ledger verb that exits 0
        having done nothing is indistinguishable from a clean audit, which
        is the silent miss this subsystem exists to prevent."""
        r = _nx_hook(verb, state=tmp_path)
        assert r.returncode == 2
        assert "usage:" in r.stderr
        assert "Traceback" not in r.stderr

    def test_reconcile_needs_its_payload_argument(self, ledger):
        r = _nx_hook("expectations_reconcile", "sess-probe", state=ledger)
        assert r.returncode == 2
        assert "payload_json" in r.stderr

    def test_an_unknown_verb_is_refused_by_the_entry_point(self, tmp_path):
        r = _nx_hook("expectations_nonsense", "sess", state=tmp_path)
        assert r.returncode == 2
        assert "unknown verb" in r.stderr


class TestTheyAreCommandTierOnly:
    def test_no_ledger_verb_is_registered_as_an_mcp_tool(self):
        """The answer IS the exit code, and an MCP tool has none. A ledger
        verb registered on the tool tier would have to smuggle its verdict
        into text for a model to re-interpret, which is how a 3 ("nothing
        checkable") becomes a 0 ("clean") in a summary."""
        registered = {spec.name for spec in HOOK_TOOLS}
        assert registered.isdisjoint(VERBS), registered & set(VERBS)

    def test_every_verb_propagates_its_exit_code(self):
        for verb in VERBS:
            assert verb in VERB_TABLE, verb
            assert verb in LEDGER_VERBS, (
                f"{verb} would have its exit code forced to 0, discarding "
                "the answer"
            )
