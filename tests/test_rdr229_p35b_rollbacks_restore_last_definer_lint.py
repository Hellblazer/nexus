# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.5b (nexus-p2m1x.24): every vectors-077/078 rollback restores its object's last definer before P3.5b.

WHY THIS EXISTS. P3.5b's changesets redefine about ninety functions, views and one trigger, each with a ``<rollback>`` that must
recreate the object exactly as its previous definer left it. A combine of two agents' edits once put a fix into 077-50's
rollback instead of its forward body: the forward bug stayed, and every rollback test that walks back through vectors-077 failed
on a body that no longer compiled. Nothing in the changeset lints checks what a rollback restores, only its shape
(tests/test_changelog_rollback_lint.py), and SchemaRollbackRoundTripIntegrationTest does not compare function bodies.

THE RULE, per changeset of vectors-077 and vectors-078:
1. it has exactly one ``<rollback>``, and its forward SQL is not the same as its rollback (a no-op changeset);
2. an id twin (``<name>_by_id``, a new function) rolls back by dropping exactly that twin;
3. every other object it creates or replaces is recreated by the rollback exactly as the object's last definer wrote it, where
   the last definer is the last CREATE of that object in db.changelog-master.xml include order with P3.5b's own two files left
   out. The comparison unescapes XML entities, drops ``--`` comments and collapses whitespace, and treats CREATE and CREATE
   OR REPLACE as the same statement; anything else that differs is a finding.
A changeset whose rollback is a DO block is reported for a hand read. P4.1, which redefines these objects again, inherits the
guard by extending OWN to its own files.
"""
from __future__ import annotations

import html
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

_RES = Path(__file__).resolve().parents[1] / "service" / "src" / "main" / "resources"
OWN = ("db/changelog/vectors-077-rdr229-p35b-readers-both.xml", "db/changelog/vectors-078-rdr229-p35b-readers-continue.xml")
_OBJ = re.compile(r"CREATE\s+(?:OR\s+REPLACE\s+)?(FUNCTION|VIEW|TRIGGER)\s+(?:IF\s+NOT\s+EXISTS\s+)?((?:nexus\.)?\w+)", re.I)
_DOLLAR = re.compile(r"\$[A-Za-z_]*\$")


def _norm(s: str) -> str:
    s = re.sub(r"--[^\n]*", "", s)
    s = html.unescape(s.replace("<![CDATA[", "").replace("]]>", ""))
    s = re.sub(r"\s+", " ", s).strip().rstrip(";").strip().lower()
    return s.replace("create or replace function", "create function").replace("create or replace view", "create view")


def _statements(text: str) -> dict[str, str]:
    """kind:name -> the text of its LAST CREATE in ``text``: to the end of its dollar-quoted body, else the next ';' or '</sql>'."""
    out: dict[str, str] = {}
    for m in _OBJ.finditer(text):
        key = m.group(1).lower() + ":" + m.group(2).lower().removeprefix("nexus.")
        dq = _DOLLAR.search(text, m.end())
        semi = text.find(";", m.end())
        if dq and (semi < 0 or dq.start() < semi):
            close = text.find(dq.group(0), dq.end())
            end = text.find(";", close + len(dq.group(0))) if close > 0 else len(text)
        else:
            end = semi
        end = end if end > 0 else len(text)
        stop = text.find("</sql>", m.end())
        if 0 <= stop < end:
            end = stop
        out[key] = text[m.start():end]
    return out


def _forward_only(raw: str) -> str:
    raw = re.sub(r"<!--.*?-->", "", raw, flags=re.S)
    return html.unescape(re.sub(r"<rollback>.*?</rollback>", "", raw, flags=re.S))


def check(res: Path = _RES, overrides: dict[str, str] | None = None) -> tuple[int, list[str], list[str]]:
    """(objects checked, problems, changesets to read by hand). ``overrides`` replaces a file's text by its include path."""
    overrides = overrides or {}

    def read(inc: str) -> str | None:
        if inc in overrides:
            return overrides[inc]
        p = res / inc
        return p.read_text() if p.exists() else None

    master = re.sub(r"<!--.*?-->", "", read("db/changelog/db.changelog-master.xml") or "", flags=re.S)
    last: dict[str, str] = {}
    for inc in re.findall(r'<include\s+file="([^"]+)"', master):
        if inc in OWN:
            continue
        text = read(inc)
        if text is not None:
            last.update(_statements(_forward_only(text)))

    checked, problems, manual = 0, [], []
    for inc in OWN:
        text = re.sub(r"<!--.*?-->", "", read(inc) or "", flags=re.S)
        for cs in re.finditer(r'(?s)<changeSet id="([^"]+)".*?</changeSet>', text):
            cid, body = cs.group(1), cs.group(0)
            parts = body.split("<rollback>")
            if len(parts) != 2:
                problems.append(f"{cid}: {len(parts) - 1} <rollback> blocks")
                continue
            fwd = html.unescape(re.sub(r"<comment>.*?</comment>", "", parts[0], flags=re.S))
            rb = html.unescape(parts[1].split("</rollback>")[0])
            if _norm(fwd.split("<sql", 1)[-1]) == _norm(rb.split("<sql", 1)[-1]):
                problems.append(f"{cid}: its forward SQL equals its rollback (a no-op changeset)")
            objects = _statements(fwd)
            twins = [k.split(":", 1)[1] for k in objects if k.endswith("_by_id")]
            if twins:
                dropped = re.search(r"DROP\s+FUNCTION\s+IF\s+EXISTS\s+nexus\.(\w+)\s*\(", rb, re.I)
                if not dropped or dropped.group(1).lower() != twins[0]:
                    problems.append(f"{cid}: twin {twins[0]}, but its rollback drops {dropped.group(1) if dropped else 'nothing'}")
                checked += 1
                continue
            if re.search(r"\bDO\s+\$", rb):
                manual.append(f"{cid}: a DO-block rollback ({', '.join(objects) or '?'})")
                continue
            restored = _statements(rb)
            for key in objects:
                if key not in last:
                    problems.append(f"{cid}: {key} has no definer before P3.5b")
                elif key not in restored:
                    problems.append(f"{cid}: its rollback does not recreate {key}")
                elif _norm(restored[key]) != _norm(last[key]):
                    problems.append(f"{cid}: the rollback of {key} differs from its last definer before P3.5b")
                else:
                    checked += 1
    return checked, problems, manual


def test_every_rollback_restores_its_last_definer():
    checked, problems, manual = check()
    assert checked > 80, f"non-vacuity: only {checked} objects checked across vectors-077/078"
    assert not problems, "P3.5b rollbacks that do not restore their last definer:\n  " + "\n  ".join(problems)
    assert not manual, "DO-block rollbacks to read by hand, then cover here:\n  " + "\n  ".join(manual)


def test_a_hunk_planted_in_a_rollback_is_caught():
    """The fault this test exists for: an edit that lands in a changeset's <rollback> instead of its forward body."""
    inc = OWN[0]
    text = (_RES / inc).read_text()
    m = re.search(r'(?s)<changeSet id="vectors-077-50".*?</changeSet>', text)
    assert m, "non-vacuity: vectors-077-50 is in vectors-077"
    forward, rollback = m.group(0).split("<rollback>", 1)
    assert "p_quarantined_at" in rollback, "non-vacuity: the planted edit has something to change"
    planted = text[:m.start()] + forward + "<rollback>" + rollback.replace("p_quarantined_at", "v_quar_id", 1) + text[m.end():]
    _, problems, _ = check(overrides={inc: planted})
    assert any(p.startswith("vectors-077-50:") and "differs from its last definer" in p for p in problems), problems


def test_a_no_op_changeset_and_a_twin_that_drops_the_wrong_function_are_caught():
    noop = ('<databaseChangeLog><changeSet id="x-1"><sql>CREATE OR REPLACE FUNCTION nexus.f() RETURNS int LANGUAGE sql AS $$ '
            'SELECT 1 $$;</sql><rollback><sql>CREATE OR REPLACE FUNCTION nexus.f() RETURNS int LANGUAGE sql AS $$ SELECT 1 $$;'
            '</sql></rollback></changeSet>'
            '<changeSet id="x-2"><sql>CREATE FUNCTION nexus.g_by_id() RETURNS int LANGUAGE sql AS $$ SELECT 1 $$;</sql>'
            '<rollback><sql>DROP FUNCTION IF EXISTS nexus.h_by_id()</sql></rollback></changeSet></databaseChangeLog>')
    _, problems, _ = check(overrides={OWN[0]: noop, OWN[1]: "<databaseChangeLog/>"})
    assert any(p.startswith("x-1:") and "no-op" in p for p in problems), problems
    assert any(p.startswith("x-2:") and "drops h_by_id" in p for p in problems), problems
