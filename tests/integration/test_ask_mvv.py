# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-245 Phase 1 Minimum Viable Validation on the real engine (nexus-wc4jj.11).

The RDR's MVV and Test Plan ("Integration (MVV) on a real engine, port 0, with a pty for the TTY
path"): the halcyon board entry is written to T2 ``threads`` through the engine, ``nx ask`` runs as a
real subprocess against it, and the asker's drain hook (``conexus/hooks/scripts/mailbox_drain.py``)
reads the result back. Nothing here is stubbed: the T2 reader/writer, the tuple writer, the mailbox
and the answer-record GET are the engine's own routes.

Four engine tests and one bound:

* ``test_steps_1_to_4_...``: the ``--via cli`` path under a pty (POSIX only), then the drain frame, then
  the stale-version and deleted-ask arms, then a second cli answer that must reuse the minted sender id. Run without a T1 session, as Sam at a bare terminal, so the
  engine stamps nothing and the frame says "no stamp".
* ``test_mediator_answer_...``: ``--via mediator`` without a pty and without ``--note``, so the frame's
  words can only have come from the record's ``sam_words``.
* ``test_a_session_carrying_a_t1_token_...``: the stamp control. The same mediator call from a process
  holding the fixture's T1 token must make the frame print that session's id, so "no stamp" elsewhere is
  a measured absence.
* ``test_step_5_...``: an ask with ``after:`` is refused and writes nothing, on both ``--via`` routes (the
  cli arm without a pty); the mediator call succeeds once the line is gone.

The assertions pin what the RDR requires (the refusal's reason, the versions and text it prints, the ref in
the sent line, a canonical UUID sender), not the CLI's exact sentences.

The only skip this module declares is the Windows one on the pty test, and
``test_the_only_declared_skip_is_the_windows_pty_one`` bounds it. A skip raised by a fixture (the engine
substrate absent on CI) is not counted by that test; the suite's engine-substrate guard owns it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse

import pytest

from nexus.commands._helpers import t2_handle
from nexus.db.t2.http_tuple_store import HttpTupleStore
from nexus.db.t2.records import TupleRow
from tests._child_encoding import PYTHON_CHILD_ENCODING
from tests._platform import IS_WINDOWS, posix_only

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "asks"
DRAIN_HOOK = Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "scripts" / "mailbox_drain.py"

ASKER = "3f2a9c1e-5b7d-4e2a-9c31-0d8e6f4a2b17"  # the session: line of tests/fixtures/asks/halcyon.txt
ASK_ID = "d1105-k9kr9-keying"
ENTRY = "halcyon"
REF = f"{ENTRY}/{ASK_ID}"
FRAME_OPEN = "An unverified claim that Sam answered your ask"
WORDS = "accept the gap on k9kr9"
GROUPS = ("interrupt", "batch", "quiet", "conditional", "answered")
_HEX16 = re.compile(r"[0-9a-f]{16}")
_SESSION_ENV = ("NX_T1_SESSION", "NX_T1_SESSION_ID", "NX_SESSION_ID", "CLAUDE_CODE_SESSION_ID")
_MAX_SKIP = 1


# --- plumbing -----------------------------------------------------------------------------------


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _version_of(entry_text: str) -> str:
    """The ask's version computed from the RDR rule (RDR-245 "Version"), not through the product: SHA-256
    over the block's lines from ``ask:`` on, each stripped, ``by:`` left out, joined with a newline, first
    16 hex characters. The block is the rest of the entry here, which holds one ask."""
    lines = entry_text.split("\n")
    first = next(n for n, line in enumerate(lines) if line.startswith("ask: "))
    kept = [s for s in (line.strip() for line in lines[first:]) if s and not re.match(r"by\s*:", s, re.IGNORECASE)]
    return hashlib.sha256("\n".join(kept).encode("utf-8")).hexdigest()[:16]


def _put(project: str, title: str, content: str) -> None:
    with t2_handle() as db:
        db.memory.put(project=project, title=title, content=content, tags="ask-mvv")


def _rows(project: str) -> list[dict]:
    with t2_handle() as db:
        return [dict(r) for r in db.memory.get_all(project=project)]


def _mailbox(session: str) -> list[TupleRow]:
    return list(HttpTupleStore().rd(f"mailbox/{session}", None, n=50))


def _env(tmp_path: Path, *, prod_write: bool, session: dict[str, str] | None = None) -> dict[str, str]:
    """A child's environment: the engine endpoint and token the fixture exported, a tmp config dir and tmp
    home, none of the session ids the suite or an outer Claude session carries, and *session* on top."""
    from tests._engine_substrate import ensure_engine  # noqa: PLC0415 -- heavy import, needed once per env

    url, token = os.environ.get("NX_SERVICE_URL", ""), os.environ.get("NX_SERVICE_TOKEN", "")
    assert urlparse(url).hostname in ("127.0.0.1", "localhost", "::1"), f"NX_SERVICE_URL is not loopback: {url!r}"
    assert url.rstrip("/") == str(ensure_engine()["base_url"]).rstrip("/"), f"{url!r} is not the test engine"
    assert token, "NX_SERVICE_TOKEN is empty: the tenant fixture did not run"
    env = {k: v for k, v in os.environ.items() if k not in _SESSION_ENV and k != "XDG_STATE_HOME"}
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env["HOME"] = env["USERPROFILE"] = str(home)
    env["NEXUS_CONFIG_DIR"] = str(tmp_path / "config")
    env.pop("NX_ALLOW_PROD_WRITE", None)
    if prod_write:
        # The CLI runs from this dev checkout, so the production-write guard presumes a real store. The
        # engine just checked above is the pytest session's throwaway substrate; the suite's own
        # in-process opt-in does not reach a child process.
        env["NX_ALLOW_PROD_WRITE"] = "ask-mvv test: the engine is the pytest session's loopback throwaway substrate"
    env.update(session or {})
    return env


def _nx_env(tmp_path: Path, session: dict[str, str] | None = None) -> dict[str, str]:
    return _env(tmp_path, prod_write=True, session=session)


def _nx(args: list[str], env: dict[str, str], cwd: Path, stdin: int = subprocess.DEVNULL) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", "from nexus.cli import main; main()", *args],
        stdin=stdin, capture_output=True, encoding=PYTHON_CHILD_ENCODING, env=env, cwd=cwd, timeout=120,
    )


def _nx_under_pty(args: list[str], env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run ``nx`` with a real pseudo-terminal as stdin, so ``isatty(stdin)`` is true in the child."""
    import pty  # noqa: PLC0415 -- POSIX only; imported here so the module still imports on Windows

    master, slave = pty.openpty()
    try:
        assert os.isatty(slave), "the pty slave is not a terminal; the TTY step would prove nothing"
        return _nx(args, env, cwd, stdin=slave)
    finally:
        os.close(slave)
        os.close(master)


def _show(env: dict[str, str], cwd: Path, ref: str = REF) -> dict:
    r = _nx(["ask", "show", ref, "--json"], env, cwd)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def _list(env: dict[str, str], cwd: Path) -> dict:
    r = _nx(["ask", "list", "--json"], env, cwd)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def _groups_of(data: dict, ref: str) -> list[str]:
    """The groups of ``nx ask list --json`` that carry *ref*, one entry per occurrence."""
    return [g for g in GROUPS for a in data[g] if a["ref"] == ref]


def _mediator_call(ref: str, version: str) -> list[str]:
    return ["ask", "answer", ref, "1", "--expect", version, "--via", "mediator", "--sam-words", WORDS]


def _drain(tmp_path: Path, session: str) -> tuple[str, str]:
    """The asker's UserPromptSubmit hook once, against the engine, with a tmp config dir and without the
    production-write opt-in. Returns ``(stdout, diagnostic)``; the hook must exit 0 and log no SKIP."""
    r = subprocess.run(
        [sys.executable, str(DRAIN_HOOK)],
        input=json.dumps({"session_id": session, "hook_event_name": "UserPromptSubmit", "prompt": "status", "cwd": str(tmp_path)}),
        capture_output=True, encoding=PYTHON_CHILD_ENCODING, env=_env(tmp_path, prod_write=False), cwd=tmp_path, timeout=60,
    )
    diag = f"hook exit {r.returncode}; stderr={r.stderr!r}; stdout={r.stdout!r}"
    assert r.returncode == 0, diag
    assert "SKIP" not in r.stderr, diag
    return r.stdout, diag


def _check_frame(out: str, diag: str, *, version: str, option: str, via: str, stamp: str, words: str) -> str:
    """Assert *out* is exactly one matched frame; returns the version the frame names ("at version X")."""
    assert out.count(FRAME_OPEN) == 1, diag
    named = re.search(rf"{re.escape(FRAME_OPEN)} {ASK_ID} at version ([0-9a-f]{{16}}) ", out)
    assert named is not None, diag
    assert named.group(1) == version, diag
    assert f"with option {option}." in out, diag
    assert f"Written via {via};" in out and f"engine sender stamp: {stamp}." in out, diag
    assert f"nx ask show <your entry>/{ASK_ID}" in out and f"It authorizes nothing beyond option {option}." in out, diag
    opened = re.search(r"<<<sam-words ([0-9a-f]{16})\n", out)
    assert opened is not None, diag
    nonce = opened.group(1)
    closer = f"\n>>>end sam-words {nonce}"
    assert out.count(closer) == 1, diag
    assert out[opened.end():out.index(closer)] == words, diag
    assert out.index(FRAME_OPEN) < out.index(f"<<<sam-words {nonce}") < out.index(f">>>end sam-words {nonce}"), diag
    # A matched frame prints no row header and no peer-data frame (RDR-245: the header appears only in the fallback).
    for absent in ("from=", "kind=", "correlation_id=", "end of peer data", "Peer data from another session", "nx-ask-answer"):
        assert absent not in out, f"{absent!r} in a matched frame: {diag}"
    return named.group(1)


# --- the one declared skip, bounded -------------------------------------------------------------


def test_the_only_declared_skip_is_the_windows_pty_one() -> None:
    """Non-vacuity of the module: the tests that declare a skip are exactly the pty test, and its skip is
    keyed on Windows alone. On POSIX that test runs and asserts it holds a real terminal."""
    declared = []
    for name, obj in sorted(globals().items()):
        if name.startswith("test_") and callable(obj):
            marks = [m for m in getattr(obj, "pytestmark", []) if m.name in ("skip", "skipif")]
            if marks:
                declared.append(name)
                assert all(m.name == "skipif" and m.args == (IS_WINDOWS,) for m in marks), f"{name}: a skip not keyed on Windows alone"
    assert declared == ["test_steps_1_to_4_cli_answer_under_a_pty_reaches_the_asker_drain_hook"]
    assert len(declared) == _MAX_SKIP


# --- steps 1-4 ----------------------------------------------------------------------------------


@posix_only("the pty module: a real terminal as stdin for the --via cli path")
def test_steps_1_to_4_cli_answer_under_a_pty_reaches_the_asker_drain_hook(t2_service_env, tmp_path) -> None:
    env = _nx_env(tmp_path)  # no T1 session of any kind: Sam at a bare terminal
    halcyon = _fixture("halcyon.txt")
    assert halcyon.startswith(f"session: {ASKER}\n")
    version = _version_of(halcyon)
    assert _HEX16.fullmatch(version)

    # Step 1: the entry is on the board; `nx ask list --json` shows the ask with the version the RDR rule gives.
    _put("threads", ENTRY, halcyon)
    data = _list(env, tmp_path)
    assert _groups_of(data, REF) == ["batch"], "an unanswered, non-blocking, unconditional ask waits in the batch"
    ask = next(a for a in data["batch"] if a["ref"] == REF)
    assert ask["version"] == version
    assert ask["session_id"] == ASKER and ask["question"].startswith("Retry-safe keying for quarantine restore")
    assert [o["n"] for o in ask["options"]] == [1, 2] and ask["options"][1]["irreversible"] is True
    assert ask["reversible"] is False and ask["conditional"] is False and ask["after"] == ""
    assert _rows("asks") == [] and _mailbox(ASKER) == [], "nothing is answered or mailed yet"
    answer_title = f"{ASKER}/{ASK_ID}/{version}"

    # Step 2: without a TTY the call is refused and writes nothing, so the pty below is what lets it through.
    answer = ["ask", "answer", REF, "1", "--expect", version, "--via", "cli", "--note", "accept the gap"]
    refused = _nx(answer, env, tmp_path)  # stdin is /dev/null
    assert refused.returncode != 0 and "tty" in refused.stderr.lower(), refused.stderr  # the RDR: "stdin must be a TTY"
    assert _rows("asks") == [] and _mailbox(ASKER) == []

    # ... under a pty it writes exactly one `sent` record and exactly one mailbox tuple.
    sent = _nx_under_pty(answer, env, tmp_path)
    assert sent.returncode == 0, sent.stderr
    records = _rows("asks")
    assert [r["title"] for r in records] == [answer_title], "exactly one record, at <asker>/<ask>/<version>"
    record = json.loads(records[0]["content"])
    assert record["status"] == "sent" and record["choice"] == 1 and record["via"] == "cli"
    assert record["note"] == "accept the gap" and record["sam_words"] == "accept the gap"
    assert record["tty"], "the record names the terminal it was typed at"
    assert record["error"] is None and record["batch_title"] is None
    tuples = _mailbox(ASKER)
    assert len(tuples) == 1, [t.id for t in tuples]
    mail = tuples[0]
    assert record["tuple_id"] == mail.id
    assert mail.subspace == f"mailbox/{ASKER}" and mail.keys == {"to": ASKER}
    cli_sender = str(mail.dims.get("from"))
    assert str(uuid.UUID(cli_sender)) == cli_sender != ASKER, "the cli sender is a canonical UUID, not the asker's own id"
    assert mail.dims.get("address_kind") == "session"
    assert mail.dims.get("kind") == "reply" and mail.dims.get("correlation_id") == f"ask:{ASK_ID}@{version}"
    assert mail.body == (
        f"nx-ask-answer v1\nask: {ASK_ID}\nversion: {version}\nchoice: 1\nvia: cli\nanswer: {record['answer_id']}\n"
    )
    assert "accept the gap" not in (mail.body or ""), "Sam's words travel in the record, never the mail"
    assert not mail.sender_session, f"the CLI ran without a T1 session, so the engine stamps nothing: {mail.sender_session!r}"
    # The sent line names the ask, the version, the choice, the route and the tuple (wording is the CLI's).
    assert all(part in sent.stdout for part in (REF, version, "choice 1", "cli", mail.id)), sent.stdout
    assert _groups_of(_list(env, tmp_path), REF) == ["answered"], "the answered ask moves to the answered group"

    # Step 3: the asker's drain hook renders the unverified-claim frame from the RECORD.
    out, diag = _drain(tmp_path, ASKER)
    mail_version = _check_frame(out, diag, version=version, option="1", via="cli", stamp="no stamp", words="accept the gap")
    assert mail.id not in out, diag
    assert _mailbox(ASKER) == [], "the drain consumed (acked) the mail"
    assert len(_rows("asks")) == 1, "draining does not touch the answer record"

    # Step 4: the ask block is edited, so the asker's re-read reports a version the frame does not carry.
    shown = _show(env, tmp_path)
    assert shown["version"] == mail_version and shown["answer"]["status"] == "sent"
    edited = halcyon.replace("asked: 2026-11-05T10:15Z", "asked: 2026-11-06T09:00Z")
    assert edited != halcyon
    _put("threads", ENTRY, edited)
    after = _show(env, tmp_path)
    assert after["version"] == _version_of(edited) != mail_version, "an edited ask must not keep the version the mail names"
    assert after["asked"] == "2026-11-06T09:00Z"
    assert after["answer"] is None, "no answer record exists for the new version: the old answer is stale"
    assert _groups_of(_list(env, tmp_path), REF) == ["batch"], "the edited ask is open again, not answered"
    # Under the pty the TTY check passes, so the version mismatch is the only refusal that can fire. It prints the current text.
    stale = _nx_under_pty(["ask", "answer", REF, "1", "--expect", mail_version, "--via", "cli"], env, tmp_path)
    assert stale.returncode != 0
    # The RDR requires the refusal to name both versions and to print the ask's current text.
    assert mail_version in stale.stderr and after["version"] in stale.stderr, stale.stderr
    assert after["question"] in stale.stderr and after["options"][0]["text"] in stale.stderr, stale.stderr
    assert len(_rows("asks")) == 1 and _mailbox(ASKER) == [], "the refused call wrote no record and no tuple"

    # ... and when the ask block is gone altogether, the asker's re-read says so.
    gone = edited.split("\nask: ")[0].replace(f"needs_sam: ask {ASK_ID}\n", "") + "\n"
    assert f"ask: {ASK_ID}" not in gone
    _put("threads", ENTRY, gone)
    missing = _nx(["ask", "show", REF], env, tmp_path)
    assert missing.returncode != 0 and REF in missing.stderr and "Traceback" not in missing.stderr, missing.stderr
    assert _groups_of(_list(env, tmp_path), REF) == []

    # Step 6, "minted once" (RDR: "for `cli` a UUID minted once in the nexus config dir"): the ask is back on the
    # board at a new version, and a second cli answer under the same config dir carries the same `from`.
    _put("threads", ENTRY, edited)
    again = _nx_under_pty(["ask", "answer", REF, "1", "--expect", after["version"], "--via", "cli"], env, tmp_path)
    assert again.returncode == 0, again.stderr
    second = _mailbox(ASKER)
    assert len(second) == 1 and second[0].id != mail.id, [t.id for t in second]
    assert second[0].dims.get("from") == cli_sender, "the cli sender id is minted once per config dir, not per call"
    assert len(_rows("asks")) == 2


# --- the mediator path and the stamp control ----------------------------------------------------


def _mediator_roundtrip(tmp_path: Path, session: dict[str, str]) -> tuple[TupleRow, str, str, str]:
    """Put the ask on the board, answer it ``--via mediator`` with *session* in the env and no ``--note``,
    drain the asker. Returns ``(mail, drained stdout, diagnostic, version)``."""
    env = _nx_env(tmp_path, session)
    halcyon = _fixture("halcyon.txt")
    version = _version_of(halcyon)
    _put("threads", ENTRY, halcyon)
    sent = _nx(_mediator_call(REF, version), env, tmp_path)
    assert sent.returncode == 0, sent.stderr
    records = _rows("asks")
    assert [r["title"] for r in records] == [f"{ASKER}/{ASK_ID}/{version}"]
    record = json.loads(records[0]["content"])
    mediator = session["NX_T1_SESSION_ID"]
    assert record["status"] == "sent" and record["choice"] == 1 and record["via"] == f"mediator:{mediator}"
    assert record["sam_words"] == WORDS and record["note"] == "" and record["tty"] is None
    tuples = _mailbox(ASKER)
    assert len(tuples) == 1 and record["tuple_id"] == tuples[0].id
    mail = tuples[0]
    assert mail.body == (
        f"nx-ask-answer v1\nask: {ASK_ID}\nversion: {version}\nchoice: 1\nvia: mediator:{mediator}\nanswer: {record['answer_id']}\n"
    )
    assert mail.dims.get("from") == mediator and mail.dims.get("address_kind") == "session" and mail.dims.get("kind") == "reply"
    assert WORDS not in (mail.body or "")
    out, diag = _drain(tmp_path, ASKER)
    return mail, out, diag, version


def test_mediator_answer_words_come_from_the_record_not_a_note(t2_service_env, tmp_path) -> None:
    mediator = str(uuid.uuid4())
    mail, out, diag, version = _mediator_roundtrip(tmp_path, {"NX_T1_SESSION_ID": mediator})
    assert not mail.sender_session, f"no T1 token was presented, so no stamp: {mail.sender_session!r}"
    # No --note was given, so the fenced words can only have been read from the record's sam_words.
    _check_frame(out, diag, version=version, option="1", via=f"mediator:{mediator}", stamp="no stamp", words=WORDS)


def test_a_session_carrying_a_t1_token_is_stamped_and_the_frame_prints_the_stamp(t2_service_env, tmp_path) -> None:
    """The stamp control: the same call as above from a process holding the fixture's T1 token. If the
    engine did not stamp, this fails, and "no stamp" in the other tests would prove nothing."""
    token, session_id = os.environ.get("NX_T1_SESSION", ""), os.environ.get("NX_T1_SESSION_ID", "")
    assert token and session_id, "the tenant fixture mints no T1 session in this substrate mode"
    stamp = str(uuid.UUID(session_id))
    mail, out, diag, version = _mediator_roundtrip(tmp_path, {"NX_T1_SESSION": token, "NX_T1_SESSION_ID": session_id})
    assert mail.sender_session == stamp, f"the engine did not stamp the minted session: {mail.sender_session!r}"
    _check_frame(out, diag, version=version, option="1", via=f"mediator:{session_id}", stamp=stamp, words=WORDS)
    assert "no stamp" not in out, diag


# --- step 5 -------------------------------------------------------------------------------------


def test_step_5_an_ask_with_an_after_line_is_refused_and_writes_nothing(t2_service_env, tmp_path) -> None:
    mediator, other = str(uuid.uuid4()), str(uuid.uuid4())
    env = _nx_env(tmp_path, {"NX_T1_SESSION_ID": mediator})
    plain = _fixture("halcyon.txt").replace(ASKER, other)
    conditional = plain.rstrip("\n") + "\nafter: engine 0.1.180 deploys\n"
    ref = f"halcyon-after/{ASK_ID}"
    _put("threads", "halcyon-after", conditional)

    data = _list(env, tmp_path)
    assert _groups_of(data, ref) == ["conditional"], "the ask with after: is listed as conditional"
    ask = next(a for a in data["conditional"] if a["ref"] == ref)
    assert ask["conditional"] is True and ask["after"] == "engine 0.1.180 deploys" and ask["session_id"] == other
    assert ask["version"] == _version_of(conditional)
    assert _rows("asks") == [] and _mailbox(other) == []

    refused = _nx(_mediator_call(ref, ask["version"]), env, tmp_path)
    assert refused.returncode != 0
    assert "after" in refused.stderr and "engine 0.1.180 deploys" in refused.stderr, refused.stderr
    assert _rows("asks") == [], "the refusal wrote no answer record"
    assert _mailbox(other) == [], "the refusal wrote no tuple"

    # The cli route, without a pty: after: is checked before the TTY, so the refusal is the after: one and not
    # the missing-terminal one. A reordering would show the TTY refusal here (or, under a pty, a send).
    cli = _nx(["ask", "answer", ref, "1", "--expect", ask["version"], "--via", "cli"], env, tmp_path)
    assert cli.returncode != 0
    assert "after" in cli.stderr and "engine 0.1.180 deploys" in cli.stderr and "tty" not in cli.stderr.lower(), cli.stderr
    assert _rows("asks") == [] and _mailbox(other) == [], "the cli refusal wrote nothing either"

    # Positive control: with the line gone (a new version) the same call goes through, so the empty reads above
    # were measurements of a working record store and mailbox.
    _put("threads", "halcyon-after", plain)
    fresh = _show(env, tmp_path, ref)
    assert fresh["after"] == "" and fresh["conditional"] is False
    assert fresh["version"] == _version_of(plain) != ask["version"]
    ok = _nx(_mediator_call(ref, fresh["version"]), env, tmp_path)
    assert ok.returncode == 0, ok.stderr
    records = _rows("asks")
    assert [r["title"] for r in records] == [f"{other}/{ASK_ID}/{fresh['version']}"]
    assert json.loads(records[0]["content"])["status"] == "sent"
    assert len(_mailbox(other)) == 1
