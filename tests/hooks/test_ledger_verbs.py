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
from nexus.hooks import expectations as exp
from nexus.hooks.ledger_verbs import VERBS
from nexus.mcp.hooks import HOOK_TOOLS

REPO_ROOT = Path(__file__).resolve().parents[2]

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


class TestTheEntryPointReturnsTheModuleExitCode:
    """The wiring, not the ledger logic (already pinned in-process by
    ``test_expectations_module.py``): a real ``nx-hook`` subprocess for
    each verb must surface the SAME exit code the module computes.

    Re-pointed at the Python entry point alone (RDR-215 bead nexus-q02nx.21):
    this used to also run the bash library as a same-fixture oracle
    (``TestTheyAgreeWithTheLibraryTheyReplace``), which is moot now that
    none of the wired ``hooks.json`` entries run it any more.
    """

    @pytest.mark.parametrize(
        "verb,expected_code",
        [("expectations_undeclared", 2), ("expectations_census", 0)],
    )
    def test_a_populated_session_exits_with_its_verdict_code(
        self, ledger, verb, expected_code
    ):
        """This fixture's ``a2``/``conexus:critic`` start has no matching
        EXPECT row: undeclared, so ``expectations_undeclared`` reports 2
        (a real deficit); ``expectations_census`` never returns 2 (its
        vocabulary is 0/1 only) and reports 0 for any populated ledger."""
        assert _nx_hook(verb, "sess-probe", state=ledger).returncode == expected_code, verb

    @pytest.mark.parametrize(
        "verb,expected_code",
        [("expectations_undeclared", 3), ("expectations_census", 0)],
    )
    def test_an_absent_session_exits_with_its_verdict_code(
        self, ledger, verb, expected_code
    ):
        """``expectations_undeclared`` reports 3: no ledger file is not
        evidence of cleanliness (nexus-ahl9v), and that distinction is the
        whole reason it is driven by exit code. ``expectations_census``
        treats a missing ledger as code 0 and silent."""
        assert (
            _nx_hook(verb, "no-such-session", state=ledger).returncode
            == expected_code
        ), verb


class TestTheSpaceAndVerifyLines:
    """The RDR-205 tuple-space cross-check lines (``SPACE_*``/``VERIFY_*``,
    nexus-em75s.19) that ``expectations_census`` appends after its TSV
    output -- the gap this bead exists to close.

    Driven through a FAKE ``nx`` (see ``fake_nx_path``/``_NO_NX_PATH``):
    this repo's dev box has a real, LIVE, shared tuple space (other
    sessions' ledgers), so the real ``nx tuple list --prefix ledger/``
    result is non-deterministic across runs and the specific branch it
    hits (BLINDSPOT vs. NEVER_RAN vs. OUTSIDE_WINDOW) depends on ambient
    state this test does not control. Each case below constructs its own
    deterministic response so the exact branch, and the exact reason
    string, is pinned rather than incidental.

    Re-pointed at the Python entry point alone (RDR-215 bead nexus-q02nx.21):
    every assertion here used to also run the bash library on the same
    fake ``nx`` and compare byte for byte, which is moot now that none of
    the wired ``hooks.json`` entries run it any more. The exact-string
    assertions below are the same ones the comparison used to license, so
    no coverage is lost.

    NOT covered by a real fixture here, and said so rather than mocked: a
    genuine 124 (deadline-exceeded) bounded-call timeout. Reaching it
    needs a fake ``nx`` that sleeps past ``NX_EXPECT_CENSUS_NX_TIMEOUT_S``,
    which would tax every run of this file by that many seconds for a
    branch whose fallback message shape (``nx tuple ... failed (rc=%d):
    %s``) every non-timeout non-zero exit here already exercises. Also
    not covered: ``nx tuple rd``'s own failure/unparseable-JSON paths --
    reaching them needs ``FAKE_NX_TEMPLATES_JSON`` to declare ``verify``
    (so ``_census_verify_absent`` proceeds past ``VERIFY_UNVERIFIABLE`` to
    the ``rd`` call at all), which is exercised below in
    ``test_a_populated_matching_subspace_and_declared_verify``.
    """

    def test_nx_absent_from_path(self, ledger):
        env = {"PATH": _NO_NX_PATH}
        mine = _nx_hook("expectations_census", "sess-probe", state=ledger, env_extra=env)
        assert "SPACE_FALLBACK\treason=PATH has no nx" in mine.stdout
        assert "VERIFY_FALLBACK\treason=PATH has no nx" in mine.stdout

    def test_unparseable_json_from_tuple_list(self, ledger, fake_nx_path):
        env = {"PATH": fake_nx_path, "FAKE_NX_LIST_JSON": "not json"}
        mine = _nx_hook("expectations_census", "sess-probe", state=ledger, env_extra=env)
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
        assert "SPACE_FALLBACK\treason=nx tuple list --json did not return an array" in mine.stdout

    def test_tuple_list_nonzero_exit(self, ledger, fake_nx_path):
        env = {
            "PATH": fake_nx_path,
            "FAKE_NX_LIST_RC": "3",
            "FAKE_NX_LIST_JSON": "engine unreachable\nretrying...",
        }
        mine = _nx_hook("expectations_census", "sess-probe", state=ledger, env_extra=env)
        # The multi-line reason must fold to ONE line (tab/newline scrubbed).
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


class TestABrokenLockIsDisclosedNotSilentlyIgnored:
    """The bead .15 review finding, pinned in both directions.

    ``_acquire_owes_lock`` used to ``return True`` on any non-EEXIST
    ``OSError`` -- an unwritable or missing state dir -- which told the
    caller "lock acquired" and sent it into the credit consult unlocked,
    with nothing on stderr. Bash makes no such distinction: a failed
    ``mkdir`` is a failed attempt whatever the cause, the loop runs its
    budget, and exhaustion is DISCLOSED (a named stderr line and
    ``cause=lock-exhausted``).

    That direction matters more than the mechanism. This module's own
    posture is that over-blocking is explicable and a silent miss is the
    failure the subsystem exists to prevent, and the old branch inverted
    it for exactly the case where the lock is BROKEN rather than
    contended -- the case least likely to be noticed.
    """

    def test_an_unusable_lock_dir_exhausts_rather_than_reporting_acquired(
        self, monkeypatch, tmp_path
    ):
        """EACCES on every mkdir must run the budget out and return False,
        which is what routes the caller to the disclosed cause."""
        monkeypatch.setenv("NX_EXPECT_LOCK_TRIES", "2")
        monkeypatch.delenv("NX_EXPECT_LOCK_DISABLE", raising=False)

        def _refuse(path, *a, **k):
            raise PermissionError(13, "Permission denied", path)

        monkeypatch.setattr(exp.os, "mkdir", _refuse)
        assert exp._acquire_owes_lock(str(tmp_path / "x.lock")) is False, (
            "a broken lock must exhaust and be disclosed, never report acquired"
        )

    def test_the_owes_verdict_carries_the_disclosed_cause(
        self, monkeypatch, tmp_path
    ):
        """End to end: a broken lock produces owes=True with
        cause=lock-exhausted, not a silent normal consult."""
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        monkeypatch.setenv("NX_EXPECT_LOCK_TRIES", "2")
        monkeypatch.delenv("NX_EXPECT_LOCK_DISABLE", raising=False)
        d = tmp_path / "nexus" / "orchestration"
        d.mkdir(parents=True)
        (d / "sess-lock.expectations").write_text(
            "2026-09-19T00:00:00Z\tEXPECT\tconexus:developer\tbackground\td1\n"
        )

        def _refuse(path, *a, **k):
            raise PermissionError(13, "Permission denied", path)

        monkeypatch.setattr(exp.os, "mkdir", _refuse)
        verdict = exp.expectations_owes_report(
            "sess-lock", "a1", "conexus:developer"
        )
        assert verdict.owes is True
        assert verdict.cause == "lock-exhausted", (
            f"a broken lock must disclose its cause, got {verdict.cause!r}"
        )

    def test_a_contended_lock_still_exhausts_the_same_way(self, monkeypatch, tmp_path):
        """The ordinary contention path is unchanged -- this fix must not
        turn a FileExistsError into something else."""
        monkeypatch.setenv("NX_EXPECT_LOCK_TRIES", "2")
        monkeypatch.delenv("NX_EXPECT_LOCK_DISABLE", raising=False)
        lockdir = tmp_path / "held.lock"
        lockdir.mkdir()
        assert exp._acquire_owes_lock(str(lockdir)) is False
