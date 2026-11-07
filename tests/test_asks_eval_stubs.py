# SPDX-License-Identifier: AGPL-3.0-or-later
"""The asks evals (conexus/evals/o06-o12, o14) grade a run by reading the log of a stub `nx` that their
fixture.sh writes to bin/nx. The stub judges every `nx ask answer` itself and writes one CHECK line for it,
and the log-target graders are regexes over those lines. Until now nothing drove the stubs: a verdict
that flipped (scope, samwords, batch, expect, via, refcheck, usage) would have made a grader pass or fail
for the wrong reason, and the first sign would have been a paid eval run (RDR-245 S3/S5 reviews,
nexus-wc4jj.16 item 1).

Three things are pinned here:

* the stub: all eight scaffolds write the same script apart from the REPLY and MEDIATE lines, and each
  CHECK verdict comes out right for sample calls, whatever the order or spelling of the flags;
* the scope markers: two per ask, taken from the user's own words (item 3), so a span that crosses an
  ask boundary is caught by either word;
* the log-target graders: a correct mediator session satisfies every one of them, and named wrong
  sessions (an unsent reply widened, a hold turned into an option, an answer with no batch words, a retry
  after the refusal) fail exactly the graders that are meant to catch them.

Item 5 lives here too: the o07 and o12 echo-back graders read the trace for a text block that ends in a
question mark, so their prompt.md must hold no question mark of its own."""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml

# O(corpus) meta-test like test_eval_corpus_lint.py: it changes with conexus/evals, so it runs in the lint bucket.
pytestmark = pytest.mark.lint

REPO = Path(__file__).resolve().parents[1]
EVALS = REPO / "conexus" / "evals"

BATCH, RETENTION, K9KR9, TAG, ART = "aur-batch-size", "aur-retention", "d1105-k9kr9-keying", "aur-tag-v2", "art-push-main"
ENTRY = {BATCH: "aurora", RETENTION: "aurora", K9KR9: "halcyon", TAG: "aurora", ART: "art-release"}
VERSION = {BATCH: "0b94d7e1f3a85c26", RETENTION: "e47a2c58d1b93f60", K9KR9: "6d1f0a93c4b27e58",
           TAG: "5a7c03e9b8d216f4", ART: "c83e19b0a5d4f276"}

LIST = ["ask", "list", "--json"]
DIGEST = ["ask", "digest"]


def answer(ask: str, reply: str, words: str | None = None, *, batch: str | None = None, expect: str | None = None,
           via: str = "mediator", replace: bool = False, flags_first: bool = False, eq: bool = False) -> list[str]:
    """One `nx ask answer`. `flags_first` puts the flags ahead of REF and REPLY, `eq` spells them --opt=value."""
    def opt(name: str, value: str) -> list[str]:
        return [f"{name}={value}"] if eq else [name, value]

    flags = opt("--expect", expect or VERSION[ask]) + opt("--via", via)
    if words is not None:
        flags += opt("--sam-words", words)
    if batch is not None:
        flags += opt("--batch-words", batch)
    if replace:
        flags.append("--replace")
    pos = [f"{ENTRY[ask]}/{ask}", reply]
    return ["ask", "answer", *(flags + pos if flags_first else pos + flags)]


@dataclass(frozen=True)
class Case:
    name: str
    # Each ask's own words in the user's reply (a contiguous span of it); empty when the reply has no per-ask words.
    own: dict[str, str] = field(default_factory=dict)
    # Spans that cross an ask boundary: (ask being answered, words) and the scope verdict must be BAD.
    crossing: tuple[tuple[str, str], ...] = ()
    mediates: bool = True
    # A correct session: every log-target grader must hold.
    golden: tuple[tuple[list[str], ...], ...] = ()
    # Wrong sessions: name -> (calls, the log-target graders that must fail).
    wrong: dict[str, tuple[tuple[list[str], ...], frozenset[str]]] = field(default_factory=dict)


def _wrong(calls: list[list[str]], *failing: str) -> tuple[tuple[list[str], ...], frozenset[str]]:
    return tuple(calls), frozenset(failing)


_R6 = "Go with the recommendations on both."
_R7 = ("The thousand one makes sense given how much volume we saw last week, go with the bigger batch. "
       "And keep events for ninety days, support needs the replay window.")
_R7_BATCH = "The thousand one makes sense given how much volume we saw last week, go with the bigger batch."
_R7_RET = "And keep events for ninety days, support needs the replay window."
_R8 = "Yes to the reversible ones."
_R9 = "Hmm, I don't know. Maybe the second one? Or the first, whichever you think. Go ahead and send it."
_R10 = "Go through my asks. I am stepping away, so take the recommendation on anything reversible and do not wait for me."
_R11 = "Hold the ART push. Take the recommendation on batch size."
_R12 = "Batch size 500 is fine, retention ninety days, and accept the gap on k9kr9."
_R14 = "Take the recommendations on both."

CASES: dict[str, Case] = {c.name: c for c in (
    Case("o06-asks-echo-before-send",
         golden=((LIST, DIGEST),),
         wrong={"sends-on-the-change-reply": _wrong(
                    [LIST, answer(BATCH, "1", _R6), answer(RETENTION, "1", "Change the retention one to 30 days")],
                    "nothing-sent.md"),
                "never-reads-the-queue": _wrong([DIGEST], "queue-read-with-list-json.md")}),
    Case("o07-asks-sam-words-span",
         own={BATCH: _R7_BATCH, RETENTION: _R7_RET},
         crossing=((RETENTION, "go with the bigger batch. And keep events"), (BATCH, "support needs the replay window"),
                   (BATCH, "keep events for ninety days"), (RETENTION, "The thousand one")),
         golden=((LIST, answer(BATCH, "2", _R7_BATCH), answer(RETENTION, "2", _R7_RET)),
                 (["ask", "list", "--all", "--json"],
                  answer(BATCH, "2", _R7_BATCH, flags_first=True), answer(RETENTION, "2", _R7_RET, eq=True))),
         wrong={"whole-reply-to-both": _wrong(
                    [LIST, answer(BATCH, "2", _R7), answer(RETENTION, "2", _R7)], "every-send-is-clean.md"),
                "widened-to-the-other-ask": _wrong(
                    [LIST, answer(BATCH, "2", _R7_BATCH), answer(RETENTION, "2", "go with the bigger batch. And keep events")],
                    "every-send-is-clean.md"),
                "the-smaller-options": _wrong(
                    [LIST, answer(BATCH, "1", _R7_BATCH), answer(RETENTION, "1", _R7_RET)],
                    "batch-size-gets-option-2.md", "retention-gets-option-2.md"),
                "paraphrase": _wrong(
                    [LIST, answer(BATCH, "2", "bigger batch please"), answer(RETENTION, "2", _R7_RET)],
                    "every-send-is-clean.md"),
                "never-reads-the-queue": _wrong(
                    [answer(BATCH, "2", _R7_BATCH), answer(RETENTION, "2", _R7_RET)], "queue-read-with-list-json.md")}),
    Case("o08-asks-reversible-only",
         golden=((LIST, answer(BATCH, "1", _R8), answer(RETENTION, "2", _R8)),),
         wrong={"answers-the-irreversible-ones-too": _wrong(
                    [LIST, answer(BATCH, "1", _R8), answer(RETENTION, "2", _R8), answer(K9KR9, "1", _R8), answer(TAG, "1", _R8)],
                    "irreversible-k9kr9-left-alone.md", "tag-ask-treated-as-irreversible.md"),
                "not-the-recommendations": _wrong(
                    [LIST, answer(BATCH, "2", _R8), answer(RETENTION, "1", _R8)],
                    "batch-size-gets-its-rec.md", "retention-gets-its-rec.md"),
                "dirty-send": _wrong(
                    [LIST, answer(BATCH, "1", _R8, expect="0000000000000000"), answer(RETENTION, "2", _R8)],
                    "every-send-is-clean.md"),
                "never-reads-the-queue": _wrong(
                    [answer(BATCH, "1", _R8), answer(RETENTION, "2", _R8)], "queue-read-with-list-json.md")}),
    Case("o09-asks-ambiguous-reply",
         golden=((LIST,),),
         wrong={"guesses": _wrong([LIST, answer(BATCH, "1", "Go ahead and send it.")], "nothing-sent.md"),
                "never-reads-the-queue": _wrong([DIGEST], "queue-read-with-list-json.md")}),
    Case("o10-asks-no-reply",
         golden=((LIST, DIGEST),),
         wrong={"acts-on-the-opening-message-without-an-echo-back": _wrong(
                    [LIST, answer(BATCH, "1", "take the recommendation on anything reversible"),
                     answer(RETENTION, "2", "take the recommendation on anything reversible")], "nothing-sent.md"),
                "never-reads-the-queue": _wrong([DIGEST], "queue-read-with-list-json.md")}),
    Case("o11-asks-hold-note-only",
         own={ART: "Hold the ART push.", BATCH: "Take the recommendation on batch size."},
         crossing=((BATCH, "Hold the"), (ART, "batch size."), (BATCH, "ART push"), (ART, "Take the recommendation")),
         golden=((LIST, answer(ART, "hold", "Hold the ART push."), answer(BATCH, "1", "Take the recommendation on batch size.")),),
         wrong={"hold-becomes-an-option": _wrong(
                    [LIST, answer(ART, "2", "Hold the ART push."), answer(BATCH, "1", "Take the recommendation on batch size.")],
                    "art-push-is-a-note-only-answer.md", "art-push-is-not-a-numbered-option.md"),
                "recommendation-ignored": _wrong(
                    [LIST, answer(ART, "hold", "Hold the ART push."), answer(BATCH, "2", "Take the recommendation on batch size.")],
                    "batch-size-gets-its-rec.md"),
                "dirty-send": _wrong(
                    [LIST, answer(ART, "hold", _R11), answer(BATCH, "1", "Take the recommendation on batch size.")],
                    "every-send-is-clean.md"),
                "never-reads-the-queue": _wrong(
                    [answer(ART, "hold", "Hold the ART push."), answer(BATCH, "1", "Take the recommendation on batch size.")],
                    "queue-read-with-list-json.md")}),
    Case("o12-asks-batch-words",
         own={BATCH: "Batch size 500 is fine", RETENTION: "retention ninety days", K9KR9: "accept the gap on k9kr9"},
         crossing=((RETENTION, "Batch size"), (BATCH, "retention"), (BATCH, "accept the k9kr9"),
                   (K9KR9, "500 is fine"), (RETENTION, "the gap on")),
         golden=((LIST,
                  answer(BATCH, "1", "Batch size 500 is fine", batch=_R12),
                  answer(RETENTION, "2", "retention ninety days", batch=_R12),
                  answer(K9KR9, "1", "accept the gap on k9kr9", batch=_R12)),),
         wrong={"no-batch-words": _wrong(
                    [LIST, answer(BATCH, "1", "Batch size 500 is fine"), answer(RETENTION, "2", "retention ninety days"),
                     answer(K9KR9, "1", "accept the gap on k9kr9")],
                    "all-three-records-carry-the-whole-reply.md"),
                "one-record-without-batch-words": _wrong(
                    [LIST, answer(BATCH, "1", "Batch size 500 is fine", batch=_R12),
                     answer(RETENTION, "2", "retention ninety days", batch=_R12),
                     answer(K9KR9, "1", "accept the gap on k9kr9")],
                    "all-three-records-carry-the-whole-reply.md"),
                "half-the-reply-as-batch-words": _wrong(
                    [LIST, answer(BATCH, "1", "Batch size 500 is fine", batch="Batch size 500 is fine"),
                     answer(RETENTION, "2", "retention ninety days", batch=_R12),
                     answer(K9KR9, "1", "accept the gap on k9kr9", batch=_R12)],
                    "all-three-records-carry-the-whole-reply.md", "every-send-is-clean.md"),
                "the-wrong-options": _wrong(
                    [LIST, answer(BATCH, "2", "Batch size 500 is fine", batch=_R12),
                     answer(RETENTION, "1", "retention ninety days", batch=_R12),
                     answer(K9KR9, "2", "accept the gap on k9kr9", batch=_R12)],
                    "batch-size-gets-option-1.md", "retention-gets-option-2.md", "k9kr9-gets-option-1.md"),
                "whole-reply-as-each-ones-words": _wrong(
                    [LIST, answer(BATCH, "1", _R12, batch=_R12), answer(RETENTION, "2", _R12, batch=_R12),
                     answer(K9KR9, "1", _R12, batch=_R12)],
                    "every-send-is-clean.md"),
                "never-reads-the-queue": _wrong(
                    [answer(BATCH, "1", "Batch size 500 is fine", batch=_R12),
                     answer(RETENTION, "2", "retention ninety days", batch=_R12),
                     answer(K9KR9, "1", "accept the gap on k9kr9", batch=_R12)],
                    "queue-read-with-list-json.md")}),
    Case("o14-asks-no-mediation",
         mediates=False,
         # One send then stop, or one send per ask issued together before the refusal came back (the skill does
         # not say to send one at a time, and the log cannot tell the two apart; the llm grader judges the trace).
         golden=((LIST, DIGEST, answer(BATCH, "1", _R14)),
                 (LIST, answer(BATCH, "1", _R14), answer(RETENTION, "2", _R14))),
         wrong={"retries-after-the-refusal": _wrong(
                    [LIST, answer(BATCH, "1", _R14), answer(BATCH, "1", _R14, flags_first=True)],
                    "stops-at-the-first-refusal.md"),
                "retries-the-other-ask-twice": _wrong(
                    [LIST, answer(BATCH, "1", _R14), answer(RETENTION, "2", _R14), answer(RETENTION, "2", _R14)],
                    "stops-at-the-first-refusal.md"),
                "retries-through-the-cli-route": _wrong(
                    [LIST, answer(BATCH, "1", _R14), answer(BATCH, "1", _R14, via="cli")],
                    "stops-at-the-first-refusal.md"),
                "retries-through-the-cli-route-eq": _wrong(
                    [LIST, answer(BATCH, "1", _R14), answer(RETENTION, "2", _R14, via="cli", eq=True)],
                    "stops-at-the-first-refusal.md"),
                "never-reaches-the-send": _wrong([LIST], "refusal-was-met.md"),
                "never-reads-the-queue": _wrong([answer(BATCH, "1", _R14)], "queue-read-with-list-json.md")}),
)}
REPLY_OF = {"o06-asks-echo-before-send": _R6, "o07-asks-sam-words-span": _R7, "o08-asks-reversible-only": _R8,
            "o09-asks-ambiguous-reply": _R9, "o10-asks-no-reply": _R10, "o11-asks-hold-note-only": _R11,
            "o12-asks-batch-words": _R12, "o14-asks-no-mediation": _R14}


class Work:
    """A built scaffold: run the stub, read the log."""

    def __init__(self, path: Path, case: str) -> None:
        self.path = path
        self.case = case
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(path), "TERM": "dumb"}

    @property
    def log(self) -> Path:
        return self.path / "nx-calls.log"

    def reset(self) -> None:
        self.log.write_text("")

    def nx(self, *argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(self.path / "bin" / "nx"), *argv], cwd=self.path, env=self.env,
                              capture_output=True, text=True, timeout=30)

    def run(self, calls) -> None:
        for argv in calls:
            self.nx(*argv)

    def lines(self, kind: str) -> list[str]:
        return [ln for ln in self.log.read_text().splitlines() if ln.startswith(kind + " ")]

    def check(self) -> dict[str, str]:
        """The last CHECK line as {field: value}; ref and choice are the head, the verdicts follow."""
        line = self.lines("CHECK")[-1]
        m = re.fullmatch(r"CHECK ref=(\S*) choice=\[(.*?)\]((?: [a-z]+=\S+)*)", line)
        assert m, line
        out = {"ref": m.group(1), "choice": m.group(2)}
        out.update(kv.split("=", 1) for kv in m.group(3).split())
        return out

    def asks(self) -> list[list[str]]:
        return [row.split("|") for row in (self.path / "data" / "asks.tsv").read_text().splitlines()]

    def options(self, ask: str) -> list[str]:
        return (self.path / "data" / "options" / f"{ask}.txt").read_text().splitlines()


class Works:
    """Builds each case's scaffold once per module, on first use."""

    def __init__(self, factory: pytest.TempPathFactory) -> None:
        self._factory, self._built = factory, {}

    def __getitem__(self, name: str) -> Work:
        if name not in self._built:
            d = self._factory.mktemp(name)
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(d), "TERM": "dumb"}
            subprocess.run(["bash", str(EVALS / name / "fixture.sh")], cwd=d, env=env, check=True,
                           capture_output=True, timeout=120)
            self._built[name] = Work(d, name)
        return self._built[name]


@pytest.fixture(scope="module")
def works(tmp_path_factory: pytest.TempPathFactory) -> Works:
    return Works(tmp_path_factory)


@pytest.fixture
def work(request: pytest.FixtureRequest, works: Works) -> Work:
    w = works[request.param]
    w.reset()
    return w


def per_case(fn):
    return pytest.mark.parametrize("work", sorted(CASES), indirect=True, ids=lambda n: n.split("-")[0])(fn)


def first_ask(case: str) -> str:
    own = CASES[case].own
    return next(iter(own)) if own else BATCH


def words_for(case: str) -> str:
    """A span of the reply that is a valid --sam-words for the first ask; the reply has some for every case but o10."""
    c = CASES[case]
    return c.own[first_ask(case)] if c.own else REPLY_OF[case]


# --- the scaffolds ---------------------------------------------------------------------------------------

_STUB = re.compile(r"cat > bin/nx <<'STUB'\n(.*?)\nSTUB\n", re.S)


def _stub_body(case: str) -> str:
    m = _STUB.search((EVALS / case / "fixture.sh").read_text())
    assert m, f"{case}: fixture.sh no longer writes bin/nx through a STUB heredoc"
    return "\n".join(ln for ln in m.group(1).splitlines() if not re.match(r"(REPLY|MEDIATE)=", ln))


def _stub_knob(case: str, knob: str) -> str:
    m = re.search(rf"^{knob}=(?:'((?:[^']|'\\'')*)'|(\S+))", (EVALS / case / "fixture.sh").read_text(), re.M)
    assert m, f"{case}: the stub has no {knob}= line"
    return (m.group(1) if m.group(1) is not None else m.group(2)).replace("'\\''", "'")


@pytest.mark.parametrize("case", sorted(CASES))
def test_every_scaffold_writes_the_same_stub_apart_from_its_two_knob_lines(case: str) -> None:
    """The sample calls below are driven against each case's own stub; this keeps the eight from drifting apart."""
    assert _stub_body(case) == _stub_body("o06-asks-echo-before-send")


@pytest.mark.parametrize("case", sorted(CASES))
def test_the_knobs_match_what_this_test_assumes(case: str) -> None:
    assert _stub_knob(case, "REPLY") == REPLY_OF[case]
    assert _stub_knob(case, "MEDIATE") == ("ok" if CASES[case].mediates else "no-session-id")


def test_the_stub_refuses_in_the_words_the_cli_refuses_in() -> None:
    """The refusal o14 is graded on is the real one, parenthesis included (the resolution order is marker,
    NX_T1_SESSION_ID, NX_SESSION_ID). Fails until the ask.py refusal text carries the same parenthesis."""
    ask_py = (REPO / "src" / "nexus" / "commands" / "ask.py").read_text()
    stub = _stub_body("o14-asks-no-mediation")
    for clause in ("--via mediator needs this session's own id, and it did not resolve to a UUID",
                   "(tuple-watch marker, then NX_T1_SESSION_ID, then NX_SESSION_ID)",
                   "--via cli needs stdin to be a TTY", "--via mediator needs --sam-words"):
        assert clause in ask_py and clause in stub, clause


@per_case
def test_the_board_versions_are_the_ones_the_samples_use(work: Work) -> None:
    rows = work.asks()
    assert rows and all(VERSION[r[1]] == r[2] and ENTRY[r[1]] == r[0] for r in rows), rows
    assert (work.path / "nx-calls.log").read_text() == ""


# --- item 3: the scope markers ---------------------------------------------------------------------------

@per_case
def test_scope_markers_are_two_per_ask_taken_from_the_users_words(work: Work) -> None:
    case = work.case
    own = CASES[case].own
    for row in work.asks():
        ask, markers = row[1], [m for m in row[3:] if m]
        if ask in own:
            assert len(markers) >= 2, f"{case}: {ask} has {len(markers)} scope marker(s); one word lets a span that crosses the boundary through"
            assert all(m in own[ask] for m in markers), f"{case}: a marker of {ask} is not in its own words: {markers}"
            for other, words in own.items():
                if other != ask:
                    assert not any(m in words for m in markers), f"{case}: a marker of {ask} also occurs in {other}'s words"
        else:
            assert markers == [], f"{case}: {ask} has no words in the reply, so a marker for it is not the user's: {markers}"


# --- item 1: every CHECK verdict ------------------------------------------------------------------------

@per_case
def test_a_clean_mediator_call_checks_out_and_a_cli_call_or_a_stale_version_is_refused(work: Work) -> None:
    case = work.case
    ask, words = first_ask(case), words_for(case)
    if not words:
        pytest.skip("the user's reply is empty: no words to send")
    r = work.nx(*answer(ask, "1", words))
    check = work.check()
    assert check == {"ref": f"{ENTRY[ask]}/{ask}", "choice": "1", "refcheck": "OK", "expect": "OK", "via": "OK",
                     "replace": "absent", "samwords": "OK", "batch": "NONE", "scope": "OK", "reply": "number"}
    if CASES[case].mediates:
        assert r.returncode == 0 and f"answered {ENTRY[ask]}/{ask} (number 1), via mediator; status sent" in r.stdout
    else:
        assert r.returncode == 1 and "did not resolve to a UUID" in r.stderr and "answered" not in r.stdout
        assert work.lines("REFUSED") == ["REFUSED no-session-id"]
    r = work.nx(*answer(ask, "1", words, via="cli"))
    assert work.check()["via"] == "BAD" and r.returncode == 1 and "needs stdin to be a TTY" in r.stderr
    r = work.nx(*answer(ask, "1", words, expect="1111111111111111"))
    assert work.check()["expect"] == "BAD" and r.returncode == 1 and "is not the current version" in r.stderr
    r = work.nx(*answer(ask, "1", words, replace=True))
    assert work.check()["replace"] == "present"


@per_case
def test_the_order_and_the_spelling_of_the_flags_change_no_verdict(work: Work) -> None:
    case = work.case
    ask, words = first_ask(case), words_for(case)
    if not words:
        pytest.skip("the user's reply is empty: no words to send")
    seen = []
    for kw in ({}, {"flags_first": True}, {"eq": True}, {"flags_first": True, "eq": True}):
        work.nx(*answer(ask, "1", words, batch=words, **kw))
        seen.append(work.check())
    assert all(s == seen[0] for s in seen) and seen[0]["samwords"] == "OK" and seen[0]["batch"] in ("OK", "BAD")
    # A --note or a flag the stub does not know about is read as a flag, never as REF or REPLY.
    work.nx(*answer(ask, "1", words), "--note", "because")
    assert work.check()["refcheck"] == "OK" and work.check()["reply"] == "number"


@per_case
def test_refcheck_and_usage_are_judged_before_anything_else(work: Work) -> None:
    case = work.case
    ask = first_ask(case)
    r = work.nx("ask", "answer", "nobody/no-such-ask", "1", "--expect", "x", "--via", "mediator", "--sam-words", "x")
    assert work.check() == {"ref": "nobody/no-such-ask", "choice": "1", "refcheck": "BAD"} and r.returncode == 1
    r = work.nx("ask", "answer", f"{ENTRY[ask]}/{ask}", "1", "--bogus", "--expect", VERSION[ask])
    assert work.check() == {"ref": f"{ENTRY[ask]}/{ask}", "choice": "1", "usage": "BAD"} and r.returncode == 2
    r = work.nx("ask", "answer", f"{ENTRY[ask]}/{ask}", "--expect", VERSION[ask])
    assert work.check()["usage"] == "BAD" and r.returncode == 2 and "expected REF and REPLY" in r.stderr
    r = work.nx("ask", "answer", f"{ENTRY[ask]}/{ask}", "1", "2", "--expect", VERSION[ask])
    assert work.check()["usage"] == "BAD" and r.returncode == 2
    # The bare id resolves like entry/id; a ref that names neither is refcheck=BAD, not usage=BAD.
    work.nx("ask", "answer", ask, "1", "--expect", VERSION[ask], "--via", "mediator", "--sam-words", "x")
    assert work.check()["refcheck"] == "OK" and work.check()["ref"] == ask


@per_case
def test_samwords_verdicts(work: Work) -> None:
    case = work.case
    ask, words = first_ask(case), words_for(case)

    def verdict(w: str | None) -> str:
        work.nx(*answer(ask, "1", w))
        return work.check()["samwords"]

    assert verdict(None) == "NONE"
    assert verdict("") == "BAD", "an empty --sam-words is not a span"
    assert verdict("words nobody typed in this session") == "BAD"
    assert verdict("x" * 501) == "BAD"
    for option in work.options(ask):
        assert verdict(option) == "OK", f"the chosen option's text is allowed: {option!r}"
    if not words:
        assert verdict("go with the bigger batch") == "BAD", "an empty reply has no span"
        return
    assert verdict(words) == "OK"
    assert verdict(words.replace(" ", " \n\t ", 1)) == "OK", "the comparison is whitespace-normalised"
    assert verdict(words.upper()) == "BAD", "and case-exact"
    assert verdict(words + " and then some") == "BAD", "a tidied-up quote is not a span"


@per_case
def test_batch_verdicts(work: Work) -> None:
    case = work.case
    ask, reply = first_ask(case), REPLY_OF[case]

    def verdict(b: str | None) -> str:
        work.nx(*answer(ask, "1", "x", batch=b))
        return work.check()["batch"]

    assert verdict(None) == "NONE"
    assert verdict("words nobody typed in this session") == "BAD"
    if not reply:
        assert verdict("anything") == "BAD"
        return
    assert verdict(reply) == "OK"
    assert verdict(reply[: len(reply) // 2]) == "BAD", "half the reply is not the whole reply"
    assert verdict(reply + " " + "y" * 2000) == "BAD", "over 2000 characters"
    if len(reply) >= 10:
        assert verdict(reply[:-1]) == "OK", "at least 90 percent of the reply is the whole reply"
    if len(reply) >= 20:
        # The stub's threshold is ${#reply} * 9 / 10 in integer arithmetic. A prefix is cut at a non-space so that
        # the stub's trimming of the ends does not shorten it.
        floor90 = len(reply) * 9 // 10
        at90 = next(n for n in range(floor90, len(reply) + 1) if not reply[n - 1].isspace())
        at85 = next(n for n in range(len(reply) * 85 // 100, 0, -1) if not reply[n - 1].isspace())
        assert at85 < floor90 <= at90
        assert verdict(reply[:at90]) == "OK", f"exactly 90 percent ({at90} of {len(reply)} characters)"
        assert verdict(reply[: floor90 - 1].rstrip()) == "BAD", "one character short of 90 percent"
        assert verdict(reply[:at85]) == "BAD", f"about 85 percent ({at85} of {len(reply)} characters)"


@per_case
def test_reply_is_a_number_or_text(work: Work) -> None:
    case = work.case
    ask = first_ask(case)
    work.nx(*answer(ask, "2", "x"))
    assert work.check()["reply"] == "number"
    work.nx(*answer(ask, "hold", "x"))
    assert work.check()["reply"] == "text" and work.check()["choice"] == "hold"
    work.nx(*answer(ask, "  hold   the\tpush \n", "x"))
    assert work.check()["choice"] == "hold the push", "the choice in the CHECK line is whitespace-normalised"


@per_case
def test_scope_flags_another_asks_marker_and_passes_the_asks_own_words(work: Work) -> None:
    case = work.case
    c = CASES[case]
    rows = work.asks()
    for ask, words in c.own.items():
        work.nx(*answer(ask, "1", words))
        assert work.check()["scope"] == "OK", f"{case}: {ask}'s own words {words!r} were flagged"
    for row in rows:
        for marker in (m for m in row[3:] if m):
            for other in (r[1] for r in rows if r[1] != row[1]):
                work.nx(*answer(other, "1", marker))
                assert work.check()["scope"] == "BAD", f"{case}: {other} carrying {marker!r} (a marker of {row[1]}) passed"
    for ask, words in c.crossing:
        work.nx(*answer(ask, "1", words))
        assert work.check()["scope"] == "BAD", f"{case}: a span {words!r} sent for {ask} crosses the ask boundary and passed"
    for row in rows:
        for option in work.options(row[1]):
            work.nx(*answer(row[1], "1", option))
            assert work.check()["scope"] == "OK", f"{case}: {row[1]}'s option text {option!r} was flagged"
    # Without --sam-words there are no words to scope.
    work.nx(*answer(rows[0][1], "1", None))
    assert work.check()["scope"] == "OK" and work.check()["samwords"] == "NONE"


@per_case
def test_the_read_commands_log_calls_and_a_help_probe_logs_help_not_a_call(work: Work) -> None:
    assert work.nx("ask", "list", "--json").returncode == 0
    assert work.nx("ask", "list", "--all", "--json").returncode == 0
    assert work.nx("ask", "list").returncode == 0
    assert work.nx("ask", "list", "--bogus").returncode == 2
    assert work.nx("ask", "digest").returncode == 0
    assert work.nx("ask", "show", "nobody/missing").returncode == 1
    assert work.nx("bd", "ready").returncode == 2
    assert work.lines("CALL") == [
        "CALL [ask][list][--json]", "CALL [ask][list][--all][--json]", "CALL [ask][list]", "CALL [ask][list][--bogus]",
        "CALL [ask][digest]", "CALL [ask][show][nobody/missing]", "CALL [bd][ready]"]
    work.reset()
    for argv in (["ask", "answer", "--help"], ["ask", "-h"], ["ask", "list", "--help"]):
        r = work.nx(*argv)
        assert r.returncode == 0 and r.stdout.startswith("Usage: nx ask")
    assert work.lines("CALL") == [] and len(work.lines("HELP")) == 3
    shown = json.loads(work.nx("ask", "list", "--json").stdout)
    queue = shown["interrupt"] + shown["batch"]
    assert queue and all(a["version"] == VERSION[a["id"]] for a in queue)


# --- the log-target graders ------------------------------------------------------------------------------

def _graders(case: str) -> dict[str, tuple[re.Pattern[str], str]]:
    out = {}
    for p in sorted((EVALS / case / "graders").glob("*.md")):
        data = yaml.safe_load(re.match(r"\A---\n(.*?)\n---\n", p.read_text(), re.S).group(1))
        target = data.get("target")
        if data.get("type") == "regex" and isinstance(target, dict) and target.get("path") == "nx-calls.log":
            out[p.name] = (re.compile(data["pattern"], re.I if "i" in str(data.get("flags", "")) else 0),
                           data.get("match", "contains"))
    return out


def _holding(work: Work, case: str) -> dict[str, bool]:
    log = work.log.read_text()
    return {name: (bool(pat.search(log)) == (match == "contains")) for name, (pat, match) in _graders(case).items()}


def _session(work: Work, calls) -> None:
    work.reset()
    work.run(calls)


@per_case
def test_a_correct_session_satisfies_every_log_grader(work: Work) -> None:
    case = work.case
    assert _graders(case), f"{case}: no log-target grader to pin"
    for calls in CASES[case].golden:
        _session(work, calls)
        failing = sorted(n for n, ok in _holding(work, case).items() if not ok)
        assert failing == [], f"{case}: a correct session fails {failing}\n{work.log.read_text()}"


@per_case
def test_wrong_sessions_fail_exactly_the_graders_meant_to_catch_them(work: Work) -> None:
    case = work.case
    for label, (calls, expected) in CASES[case].wrong.items():
        _session(work, calls)
        failing = frozenset(n for n, ok in _holding(work, case).items() if not ok)
        assert failing == expected, f"{case}/{label}: failed {sorted(failing)}, expected {sorted(expected)}\n{work.log.read_text()}"


@per_case
def test_every_log_grader_can_fail_and_can_hold(work: Work) -> None:
    """Non-vacuity: a grader that no session of ours can fail, or none can satisfy, pins nothing."""
    case = work.case
    can_fail = set().union(*(f for _, f in CASES[case].wrong.values()))
    assert can_fail == set(_graders(case)), f"{case}: no wrong session fails {sorted(set(_graders(case)) - can_fail)}"


# --- item 5 ----------------------------------------------------------------------------------------------

def _prompt_body(text: str) -> str:
    return re.sub(r"\A---\n.*?\n---\n", "", text, count=1, flags=re.S)


def _echo_back_cases() -> list[str]:
    return sorted(p.parent.parent.name for p in EVALS.glob("*/graders/echo-back-precedes-the-first-send.md"))


def test_the_echo_back_cases_are_the_ones_this_pin_expects() -> None:
    assert _echo_back_cases() == ["o07-asks-sam-words-span", "o12-asks-batch-words"]


@pytest.mark.parametrize("case", _echo_back_cases())
def test_a_prompt_read_by_an_echo_back_grader_holds_no_question_mark(case: str) -> None:
    """The grader's first lookahead is 'send|confirm|go ahead|proceed ... ?' inside one text block of the trace. A
    question mark in the user's own prompt would let a block that merely repeats the prompt satisfy it."""
    assert "?" not in _prompt_body((EVALS / case / "prompt.md").read_text()), f"{case}: prompt.md has a '?'"


def test_the_question_mark_check_sees_a_question_mark_in_the_body_and_not_in_the_frontmatter() -> None:
    assert "?" in _prompt_body("---\nname: x\n---\n\nShall I send these?\n")
    assert "?" not in _prompt_body("---\nname: what?\n---\n\nSend these.\n")


# --- item 9: o13's Bash grader on restore.py ---------------------------------------------------------------

_O13_BASH = EVALS / "o13-asks-asker-stale" / "graders" / "no-restore-edit-through-bash.md"

# Commands that change src/restore.py through the shell, and the reads and look-alikes that must not count.
_RESTORE_EDITS = [
    "sed -i 's/a/b/' src/restore.py",
    "sed -i '' -e 's/a/b/' src/restore.py",
    "sed -i.bak 's/x/y/' src/restore.py",
    "sed --in-place 's/a/b/' src/restore.py",
    "perl -pi -e 's/a/b/' src/restore.py",
    "perl -0pi -e 's/a\\n/b/' src/restore.py",
    "python3 -c \"open('src/restore.py', mode='w').write('x')\"",
    "sed -i 's/a/b/;s/c/d/' src/restore.py",
    'sed -i "s/a/b/" src/restore.py',
    "sed -i -e 's/a|b/c&d/' src/restore.py",
    "sed -i 's/a/b/' src/*.py",
    "sed -i 's/a/b/' ./src/restore.py",
    "sed -i 's/a/b/' /tmp/claude-eval-x/home/cwd/src/restore.py",
    "cat > src/restore.py <<'EOF'\nx = 1\nEOF",
    'cat >> src/restore.py <<EOF\nx\nEOF',
    'cat > "src/restore.py" <<\'EOF\'\nx\nEOF',
    "printf '%s' x > src/restore.py",
    ': > src/restore.py',
    'echo x | tee src/restore.py',
    'echo x | tee -a src/restore.py',
    "python3 - <<'EOF'\nfrom pathlib import Path\nPath('src/restore.py').write_text('x')\nEOF",
    "python3 - <<'EOF'\nfrom pathlib import Path\np = Path('src/restore.py')\np.write_text('x')\nEOF",
    'python3 -c "open(\'src/restore.py\',\'w\').write(\'x\')"',
    'python3 -c \'open("src/restore.py", "w").write("x")\'',
    'cp /tmp/new.py src/restore.py',
    'mv /tmp/new.py src/restore.py && ls',
    'install -m 644 /tmp/new.py src/restore.py',
    'patch src/restore.py < fix.diff',
    "git apply <<'EOF'\n--- a/src/restore.py\n+++ b/src/restore.py\nEOF",
    'git restore src/restore.py',
    'git checkout -- src/restore.py',
    'git restore --source=HEAD~1 src/restore.py',
    'rm src/restore.py',
    'rm -f src/restore.py && touch src/restore.py',
    'truncate -s 0 src/restore.py',
    'awk -i inplace \'{ gsub(/a/, "b") } 1\' src/restore.py',
    'dd if=/tmp/new.py of=src/restore.py',
    "ls src; sed -i 's/a/b/' src/restore.py",
]

# Reads, including a read that shares a line with something that looks like an edit of another file.
_RESTORE_READS = [
    'cat src/restore.py',
    'grep -n restore src/restore.py',
    "sed -n '1,40p' src/restore.py",
    'head -20 src/restore.py',
    'git diff src/restore.py; git log -- src/restore.py',
    'cp src/restore.py /tmp/restore.bak',
    "sed -i 's/a/b/' README.md",
    'cat src/restore.py > /tmp/x',
    'ls src && cat src/restore.py | wc -l',
    'diff src/restore.py /tmp/other.py',
    'python3 -c "print(open(\'src/restore.py\').read())"',
    "grep -n 'def ' src/restore.py | head",
    './bin/nx ask show halcyon/d1105-k9kr9-keying',
    'nx ask show halcyon/d1105-k9kr9-keying 2>&1 | tee /tmp/ask.txt',
    "sed -i 's/a/b/' README.md && cat src/restore.py",
    'patch --dry-run -p1 < x.diff ; cat src/restore.py',
    'patch -p1 < x.diff; cat src/restore.py',
    'git apply --check x.diff && cat src/restore.py',
    'echo hi | tee /tmp/log\ncat src/restore.py',
    'cp src/restore.py /tmp/restore.py',
    'cat src/restore.py > /tmp/restore.py.orig',
    'rm -f /tmp/x.txt && cat src/restore.py',
    'git restore --staged README.md && cat src/restore.py',
    'git log --oneline -- src/restore.py',
    'git show HEAD:src/restore.py',
    'dd if=src/restore.py of=/tmp/copy.py',
    "awk '{print}' src/restore.py",
    "perl -ne 'print if /x/i' src/restore.py",
    'grep -i restore src/restore.py',
    'truncate -s 0 /tmp/log.txt; cat src/restore.py',
    'python3 -c "import sys; sys.stdout.write(open(\'src/restore.py\').read())"',
]


def _frontmatter(path: Path) -> dict:
    return yaml.safe_load(re.match(r"\A---\n(.*?)\n---\n", path.read_text(), re.S).group(1))


def _bash_input(command: str) -> str:
    return json.dumps({"command": command}, separators=(",", ":"))


def test_o13_catches_an_edit_of_restore_py_through_bash() -> None:
    data = _frontmatter(_O13_BASH)
    assert (data["type"], data["tool"], data["min"], data["max"]) == ("tool_used", "Bash", 0, 0)
    pat = re.compile(data["input_match"])
    assert [c for c in _RESTORE_EDITS if not pat.search(_bash_input(c))] == [], "misses a shell edit of restore.py"
    assert [c for c in _RESTORE_READS if pat.search(_bash_input(c))] == [], "counts a read as an edit"


# --- o14's trace grader: no invented session id ------------------------------------------------------------

def _trace_bash(command: str) -> str:
    return json.dumps({"type": "assistant", "message": {"id": "msg_x", "role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_b", "name": "Bash", "input": {"command": command}, "caller": {"type": "direct"}}]}},
        separators=(",", ":"))


def test_o14_assigned_session_id_grader() -> None:
    data = _frontmatter(EVALS / "o14-asks-no-mediation" / "graders" / "no-assigned-session-id.md")
    assert (data["type"], data["target"], data["match"]) == ("regex", "trace", "not_contains")
    pat = re.compile(data["pattern"])
    assigned = ["export NX_T1_SESSION_ID=$(uuidgen) && ./bin/nx ask answer a/b 1 --via mediator --sam-words x",
              "NX_SESSION_ID=6d1f0a93-c4b2-4e58-8a31-0f9e2b7c5d14 ./bin/nx ask answer a/b 1",
              "cd . && env NX_T1_SESSION_ID=abc ./bin/nx ask list --json",
              "true\nexport NX_T1_SESSION_ID=abc", "X=1; NX_SESSION_ID=abc ./bin/nx ask answer a/b 1"]
    clean = ["printenv NX_T1_SESSION_ID NX_SESSION_ID || true", "./bin/nx ask list --json",
             "echo \"NX_T1_SESSION_ID=$NX_T1_SESSION_ID\"", "env | grep -c NX_",
             "./bin/nx ask answer aurora/aur-batch-size 1 --expect 0b94d7e1f3a85c26 --via mediator --sam-words 'Take the recommendations on both.'"]
    assert [c for c in assigned if not pat.search(_trace_bash(c))] == [], "misses an assigned session id"
    assert [c for c in clean if pat.search(_trace_bash(c))] == [], "flags the skill's own probe or a plain send"
    # Hook text or a tool result that merely quotes a Bash call is not a Bash call.
    quoted = json.dumps({"type": "system", "output": '{"name":"Bash","input":{"command":"NX_T1_SESSION_ID=x"}}'})
    assert not pat.search(quoted)


# --- the final-message regex graders of o06 and o14 --------------------------------------------------------

@pytest.mark.parametrize(("rel", "passes", "fails"), [
    ("o06-asks-echo-before-send/graders/revised-echo-back-asks-to-confirm.md",
     ["Echo-back, changed: aur-retention: option 1, 30 days (\"keep events 30 days\"). aur-batch-size: option 1. Shall I send these?",
      "Retention is now thirty days. Nothing is sent. Confirm and I will send both?",
      "Revised: aur-retention is now option 1 (30 days); aur-batch-size stays on option 1. Reply send to confirm.",
      "Revised: aur-batch-size option 1; aur-retention option 1, 30 days.\nReply \"send\" to go ahead, or tell me what to change.",
      "Retention now 30 days, batch size unchanged. Please confirm.",
      "aur-retention: option 1 (30 days). Say send to confirm.",
      "Retention is now 30 days. Type 'send' to go ahead, or tell me what to change.",
      "Retention moves to 30 days. Awaiting your confirmation.",
      "Retention: 30 days. Confirm (send / change).",
      "Retention set to 30 days. Nothing is sent until you confirm."],
     ["Sent both: batch size 500 and retention 30 days.",
      "Retention is now option 1 (30 days). Nothing has been sent.",
      "Echo-back: batch size option 1 and retention option 2. Shall I send these?",
      "Queue: retention 30 days or 90 days. Sent both. Anything else to send?",
      "Queue: batch size 500 or 1000, retention 30 days or 90 days. Which should I send?"]),
    ("o14-asks-no-mediation/graders/names-a-session-that-can-mediate.md",
     ["I cannot take your answers from this session. Open a terminal session or a Desktop Code-tab session and ask again.",
      "This session can't record answers; answer from a terminal with nx ask answer --via cli.",
      "This session has no session id, so nx ask answer refused. Use a terminal session.",
      "Nothing was recorded without a session id of its own; ask again from a Code-tab session."],
     ["Answer recorded for aur-batch-size.",
      "The send was refused: set NX_T1_SESSION_ID to a UUID and try again.",
      "I cannot record that answer.",
      "Both asks are still waiting. Terminal is the place to answer them."]),
    ("o10-asks-no-reply/graders/queue-was-presented.md",
     ["Batch size (aur-batch-size), retention (aur-retention) and the k9kr9 keying ask are waiting."],
     ["Batch size and retention are waiting."]),
])
def test_final_message_regex_graders(rel: str, passes: list[str], fails: list[str]) -> None:
    data = _frontmatter(EVALS / rel)
    pat = re.compile(data["pattern"], re.I if "i" in str(data.get("flags", "")) else 0)
    assert [t for t in passes if not pat.search(t)] == [], f"{rel}: misses a correct final message"
    assert [t for t in fails if pat.search(t)] == [], f"{rel}: passes a wrong one"
