# SPDX-License-Identifier: AGPL-3.0-or-later
"""The census half and the report of tests/e2e/rdr229-p2r-mvv.sh (RDR-229 P2.R, nexus-p2m1x.16).

Two subcommands, both stdlib plus the repo's own census module:

``census``  prints one JSON record: the client parse census measured live (``LEDGER``, by class, holdovers by the
            Phase 2 step their note names) beside its pins, and the engine census pins read out of
            ``CollectionParseGateTest.java``. Check 5 of the MVV asks that both gates sit at their Phase 2 pins.
            The RDR states no numeric Phase 2 total, so "Phase 2 pin" is asserted as what the RDR says Phase 2
            leaves: the client's ``holdover`` class empty (interim, "until Phase 2 moves it") and its ``routing``
            class empty (P2.9, "the class is empty from P2.9 on"), the URI-string classifiers unchanged at 2 (item 1
            and P2.9: "not moved"), the engine's ``quarantine-`` literal census at zero (P2.5 took all six to zero)
            and its ``split("__")`` census at zero, and both gates green at the pins they carry (the gate tests run
            and their results ride in as arguments of ``report``).

``report``  reads the checks' results file, the census record and the two gate results, prints the per-check table
            (checks 1 to 6; 5 is the census) and the Phase 2 step each FAIL waits on. Exit 0 only when every check
            passed, 1 when a check FAILed, 2 when a check recorded ERROR: the gate's own fixture or a dependency is
            wrong (no pg_background, a seed that did not take), which says nothing about the code under test.
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
ENGINE_GATE = REPO / "service/src/test/java/dev/nexus/service/vectors/CollectionParseGateTest.java"

#: What each check waits on when it is red: the Phase 2 step (RDR-229 Approach, Phase 2) and its bead.
WAITS = {
    1: "P2.3 write paths take the chunk model from the row (nexus-p2m1x.8)",
    2: "no Phase 2 step: a pre-satisfied guard, red only if the row-preferred model resolver regressed "
       "(nexus-ml9vu, 7476beecce)",
    3: "P2.5 lifecycle from the row at registration and in the guards (nexus-p2m1x.10), "
       "P2.6 the SQL movers read columns (nexus-p2m1x.11)",
    4: "no Phase 2 step: a pre-satisfied guard, red only if the P2.1 non-stranding behaviour regressed "
       "(nexus-p2m1x.6)",
    6: "P2.10 URI moves leave chroma:// residue alone, the registered repair moves, the relaxed backfill verb "
       "(nexus-p2m1x.15)",
}
#: Checks that held before Phase 2 began: they guard against a regression and prove no Phase 2 work.
PRE_SATISFIED = frozenset({2, 4})
STEP_BEAD = {"P2.2": "nexus-p2m1x.7", "P2.4": "nexus-p2m1x.9", "P2.9": "nexus-p2m1x.14", "P2.5": "nexus-p2m1x.10",
             "P2.6": "nexus-p2m1x.11", "P2.10": "nexus-p2m1x.15"}


def _engine_pins() -> dict[str, int]:
    src = ENGINE_GATE.read_text(encoding="utf-8")
    pins = {}
    for name in ("COLLECTION_LITERAL_TOTAL_CEILING", "COLLECTION_SPLIT_TOTAL_CEILING",
                 "QUARANTINE_LITERAL_TOTAL_CEILING", "QUARANTINE_READ_TOTAL_CEILING"):
        m = re.search(rf"\b{name}\s*=\s*(\d+)\s*;", src)
        if not m:
            raise SystemExit(f"census: {name} not found in {ENGINE_GATE}: the engine gate moved, update this script")
        pins[name] = int(m.group(1))
    return pins


def census() -> None:
    sys.path.insert(0, str(REPO))  # the gate runs this isolated (-I): the repo root is not on the path
    from tests import _collection_name_census_scan as scan  # noqa: PLC0415 -- after the sys.path line above
    from tests.test_collection_name_parse_census import (  # noqa: PLC0415 -- after the sys.path line above: the gate runs isolated (-I)
        CLASS_PIN,
        LEDGER,
        PARSE_SITE_PIN,
    )

    live = Counter()
    holdover_by_step: Counter = Counter()
    for site in LEDGER:
        live[site.cls] += site.n
        if site.cls == "holdover":
            m = re.search(r"\bP2\.[0-9]+\b", site.note)
            holdover_by_step[m.group(0) if m else "unowned"] += site.n
    engine = _engine_pins()
    problems: list[dict] = []

    def need(ok: bool, what: str, waits: str) -> None:
        if not ok:
            problems.append({"what": what, "waits_on": waits})

    # Non-vacuity of the two class names this check reads: a class renamed in the census would read as an empty one.
    need({"holdover", "routing"} <= set(scan.CLASSES) and sum(live.values()) > 0
         and set(live) <= set(scan.CLASSES),
         f"the census vocabulary {sorted(scan.CLASSES)} lacks 'holdover' or 'routing', or the ledger holds a class "
         "outside it: a renamed class would read as an empty one", "whoever renamed the class")
    need(live["holdover"] == 0, f"client holdover class is {live['holdover']}, Phase 2 leaves 0 "
         f"(by step: {dict(holdover_by_step)})",
         ", ".join(f"{s} ({STEP_BEAD.get(s, '?')})" for s in sorted(holdover_by_step)) or "P2.2, P2.4")
    need(live["routing"] == 0, f"client routing class is {live['routing']}, P2.9 empties it",
         f"P2.9 routing by content type reads the row ({STEP_BEAD['P2.9']}; the client reland nexus-p2m1x.14, "
         "reverted on develop at d1e38326a)")
    need(live["uri-classifier"] == 2, f"client uri-classifier class is {live['uri-classifier']}, it stays 2",
         "none: the URI-string classifiers are not moved")
    need(engine["QUARANTINE_LITERAL_TOTAL_CEILING"] == 0,
         f"engine quarantine- literal ceiling is {engine['QUARANTINE_LITERAL_TOTAL_CEILING']}, P2.5 takes it to 0",
         f"P2.5 ({STEP_BEAD['P2.5']})")
    need(engine["COLLECTION_SPLIT_TOTAL_CEILING"] == 0,
         f"engine split(\"__\") ceiling is {engine['COLLECTION_SPLIT_TOTAL_CEILING']}, it is 0",
         "RDR-204 nexus-ft04v.16")
    need(dict(live) == CLASS_PIN and sum(live.values()) == PARSE_SITE_PIN,
         f"the ledger measures {dict(live)} and the pins say {CLASS_PIN} (total {PARSE_SITE_PIN})",
         "whichever step changed the ledger without its pin")
    sys.stdout.write(json.dumps({
        "client_live": dict(live), "client_pin": CLASS_PIN, "client_pin_total": PARSE_SITE_PIN,
        "client_holdover_by_step": dict(holdover_by_step), "engine_pins": engine, "problems": problems,
    }, sort_keys=True) + "\n")


def report(results: Path, census_json: Path, client_gate_rc: int, client_gate_n: int,
           engine_gate_rc: int, engine_gate_n: int) -> int:
    rows: dict[int, dict] = {}
    for line in results.read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        rows[rec["n"]] = rec
    cen = json.loads(census_json.read_text(encoding="utf-8"))
    fixture = rows.get(0)
    out: list[str] = ["", "RDR-229 P2.R MVV, Phase 2 half", "=" * 31]
    ok_all = True
    gate_error = False
    if fixture:
        f = fixture["evidence"]
        out.append(f"fixture: {f['name']}  row owner/model: {f['row']['owner_id']} / {f['row']['embedding_model']}  "
                   f"name owner/model: {f['name_owner']} / {f['name_model']}")
    failures: list[tuple[str, str]] = []

    def show(n: int) -> None:
        nonlocal ok_all, gate_error
        rec = rows.get(n)
        if rec is None:
            out.append(f"check {n}  NOT RUN")
            failures.append((f"check {n}", WAITS[n]))
            ok_all = False
            return
        ev = rec["evidence"]
        label = "  [pre-satisfied guard: held before Phase 2]" if n in PRE_SATISFIED else ""
        out.append(f"check {n}  {rec['status']}  {rec['name']}{label}  [examined={ev.get('examined', 0)}]")
        brief = {k: v for k, v in ev.items() if k not in ("cli_tail",)}
        out.append("          evidence: " + json.dumps(brief, sort_keys=True, default=str)[:900])
        if rec["status"] == "ERROR":
            ok_all = False
            gate_error = True
            out.append("          GATE ERROR (not a finding about the code under test): " + rec["error"].strip()[:500])
        elif rec["status"] != "PASS":
            ok_all = False
            out.append("          error: " + rec["error"].strip().splitlines()[0][:1200])
            failures.append((f"check {n}", WAITS[n]))

    for n in (1, 2, 3, 4):
        show(n)
    # check 5
    # A census gate that produced no test count is an infrastructure fault (a compile error, no surefire report, a
    # failed uv), never a FAIL of the pins: exit 2. A gate that ran tests and went red is a FAIL.
    infra = [name for name, n in (("client", client_gate_n), ("engine", engine_gate_n)) if n <= 0]
    if infra:
        gate_error = True
        ok_all = False
        out.append(f"check 5  ERROR  the {' and '.join(infra)} census gate produced no test count "
                   f"(client rc={client_gate_rc} n={client_gate_n}; engine rc={engine_gate_rc} n={engine_gate_n}): "
                   "GATE ERROR, not a finding about the pins")
    gate_ok = client_gate_rc == 0 and engine_gate_rc == 0 and not infra
    census_ok = not cen["problems"]
    status5 = "PASS" if gate_ok and census_ok else "FAIL"
    if not infra:
        out.append(f"check 5  {status5}  both census gates are at their Phase 2 pins  [client gate tests={client_gate_n} "
                   f"rc={client_gate_rc}; engine gate tests={engine_gate_n} rc={engine_gate_rc}]")
    out.append("          client ledger " + json.dumps(cen["client_live"], sort_keys=True)
               + f" of {cen['client_pin_total']}; holdover by step " + json.dumps(cen["client_holdover_by_step"], sort_keys=True))
    eng = cen["engine_pins"]
    p2 = {k: eng[k] for k in ("QUARANTINE_LITERAL_TOTAL_CEILING", "COLLECTION_SPLIT_TOTAL_CEILING")}
    info = {k: v for k, v in eng.items() if k not in p2}
    out.append("          engine Phase 2 pins (both 0 by the lead's ruling) " + json.dumps(p2, sort_keys=True))
    out.append("          engine ceilings OUTSIDE the Phase 2 pins, printed for the record, not asserted "
               + json.dumps(info, sort_keys=True))
    if not gate_ok and not infra:
        ok_all = False
        failures.append(("check 5 (a gate ran and is red)",
                         "the gate's own pin drifted from its tree: fix the pin or the tree"))
        out.append(f"          a gate ran and is red: client rc={client_gate_rc} n={client_gate_n}, "
                   f"engine rc={engine_gate_rc} n={engine_gate_n}")
    for p in cen["problems"]:
        ok_all = False
        out.append("          " + p["what"])
        failures.append(("check 5: " + p["what"], p["waits_on"]))
    show(6)
    out.append("")
    if failures:
        out.append("FAIL, and what each waits on:")
        for what, waits in failures:
            out.append(f"  - {what}: {waits}")
    out.append("RESULT: " + ("GATE ERROR" if gate_error else ("PASS" if ok_all else "FAIL")))
    sys.stdout.write("\n".join(out) + "\n")
    return 2 if gate_error else (0 if ok_all else 1)


def _main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "census":
        census()
        return 0
    if len(argv) == 8 and argv[1] == "report":
        return report(Path(argv[2]), Path(argv[3]), *(int(a) for a in argv[4:8]))
    sys.stderr.write("usage: rdr229_p2r_report.py census | report RESULTS CENSUS_JSON "
                     "CLIENT_RC CLIENT_N ENGINE_RC ENGINE_N\n")
    return 2


if __name__ == "__main__":
    try:
        code = _main(sys.argv)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 -- the gate's own tooling failed: exit 2, never read as a finding
        sys.stderr.write(f"rdr229_p2r_report.py: GATE ERROR: {type(exc).__name__}: {exc}\n")
        code = 2
    sys.exit(code)
