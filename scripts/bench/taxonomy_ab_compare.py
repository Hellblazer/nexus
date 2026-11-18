# SPDX-License-Identifier: AGPL-3.0-or-later
"""Print a saved ``taxonomy_ab.py`` leg-1 run against a gated re-run (``--gate-from``), and the gated
run's own paired gated-minus-ungated table and rule block. Reads output files; touches no engine.

    PYTHONPATH=scripts uv run python scripts/bench/taxonomy_ab_compare.py SAVED.json GATED.json
    PYTHONPATH=scripts uv run python scripts/bench/taxonomy_ab_compare.py GATED.json

Two files: (1) the runs must be the same measurement: when the sets, ``S2_collections``, the question
ids or ``n`` differ, the output is ``MISMATCH`` lines and the exit status is 1, with no table. (2) The
saved run against the ungated arms of the gated run, per set and arm; these are two separate runs, and
the ungated arms of a ``--gate-from`` run differ from the saved ones by design (a collection with no
topic is searched unscoped), so read this part as context. (3) The in-run sections below.

One file (the gated run alone, for when the live S2 drifted from the saved run): only the in-run
sections, with the S1 delta within the run standing in for the S1 drift. In both modes: the paired table
(every ``@g`` arm minus the ungated arm it is built on, per question, counted in questions), the rule
block (topk1@g0.9 decides; topk1@g0.96 and cband0.03@g0.9 are reported beside it) with PASS/FAIL and one
verdict line, the pre-registered cases in T2 nexus_rdr/134-rescore-rule-2026-11-17, and each changed question's top 3 with the collections it moved between.

A ``taxonomy_ab2.py`` (prod) output is read alone: its rule lines come from the summary's ``vs_ungated``
counts. Given as one of two files it is refused with a message. So is any file that is neither output.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

FIELDS = ("ndcg3", "recall10", "wins", "losses", "d_ndcg3")
EPS = 1e-9


def _cell(saved: dict[str, Any], gated: dict[str, Any], field: str) -> str:
    s, g = saved.get(field), gated.get(field)
    if s is None or g is None:  # OFF has no wins, losses or deltas
        return "-"
    if field in ("wins", "losses"):
        return f"{s}->{g}"
    return f"{s:.4f}->{g:.4f} ({g - s:+.4f})"


def mismatches(saved: dict[str, Any], gated: dict[str, Any]) -> list[str]:
    """What makes two runs not the same measurement: n, S2_collections, the sets, the question ids."""
    out: list[str] = []
    ss, gs = saved.get("summary", {}), gated.get("summary", {})
    for key in ("n", "S2_collections"):
        if ss.get(key) != gs.get(key):
            out.append(f"{key}: saved {ss.get(key)!r}, gated {gs.get(key)!r}")
    sa, ga = saved.get("sets") or {}, gated.get("sets") or {}
    for name in sorted(set(sa) | set(ga)):
        if sa.get(name) != ga.get(name):
            only_s = sorted(set(sa.get(name) or []) - set(ga.get(name) or []))
            only_g = sorted(set(ga.get(name) or []) - set(sa.get(name) or []))
            out.append(f"set {name}: only in saved {only_s}, only in gated {only_g}")
    qs = [r["qid"] for r in saved.get("results") or []]
    qg = [r["qid"] for r in gated.get("results") or []]
    if qs != qg:
        out.append(f"question ids: saved {len(qs)}, gated {len(qg)}; "
                   f"only in saved {sorted(set(qs) - set(qg))}, only in gated {sorted(set(qg) - set(qs))}"
                   + ("" if set(qs) != set(qg) else "; same ids in a different order"))
    return out


def _saved_vs_gated(saved: dict[str, Any], gated: dict[str, Any]) -> list[str]:
    ssum, gsum = saved["summary"], gated["summary"]
    lines: list[str] = []
    for sname in sorted(set(ssum) | set(gsum)):
        if sname in ("n", "S2_collections"):
            continue
        lines.append(f"{sname}  (saved run -> the gated run's ungated arms)")
        for arm in ssum[sname]:
            if arm not in gsum[sname]:
                lines.append(f"  {arm}: only in saved")
                continue
            lines.append(f"  {arm:<10} " + "  ".join(
                f"{f}={_cell(ssum[sname][arm], gsum[sname][arm], f)}" for f in FIELDS))
        lines += [f"  {arm}: only in gated" for arm in gsum[sname]
                  if arm not in ssum[sname] and "@g" not in arm]
    return lines


def paired(gated: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    """set -> gated arm -> {better, worse, same, n, mean_d_ndcg3, mean_d_recall10}: each ``@g`` arm
    minus the ungated arm it is built on, per question of the one run, counted in questions."""
    out: dict[str, dict[str, dict[str, Any]]] = {}
    results = gated.get("results") or []
    for sname in (gated.get("sets") or {}):
        arms = sorted({k for r in results for k in r.get(sname, {}) if "@g" in k})
        for arm in arms:
            base = arm.split("@")[0]
            dn = [r[sname][arm]["ndcg3"] - r[sname][base]["ndcg3"] for r in results]
            dr = [r[sname][arm]["recall10"] - r[sname][base]["recall10"] for r in results]
            n = len(dn)
            out.setdefault(sname, {})[arm] = {
                "better": sum(x > EPS for x in dn), "worse": sum(x < -EPS for x in dn),
                "same": sum(abs(x) <= EPS for x in dn), "n": n,
                "mean_d_ndcg3": sum(dn) / n if n else 0.0, "mean_d_recall10": sum(dr) / n if n else 0.0}
    return out


def _moves(gated: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for r in gated.get("results") or []:
        for sname in (gated.get("sets") or {}):
            for arm in sorted(k for k in r.get(sname, {}) if "@g" in k):
                a, b = r[sname][arm.split("@")[0]], r[sname][arm]
                if a["top3"] == b["top3"] and a.get("top3_coll") == b.get("top3_coll"):
                    continue
                ca, cb = a.get("top3_coll"), b.get("top3_coll")
                moved = ("; collections moved: " + ", ".join(f"{x} -> {y}" for x, y in zip(ca, cb) if x != y)
                         if ca is not None and cb is not None else "; (no per-document collections stored)")
                lines.append(f"  {r['qid']} {sname} {arm}: top3 {a['top3']} -> {b['top3']}{moved}")
    return lines


GAIN_MIN = 0.105  # gain vs the in-run OFF, mean ndcg3 over S2
WINS_MIN = 8
LOSSES_MAX = 1
WORST_MIN = -0.1  # no question worse than OFF by more than this
DRIFT_FLOOR = 0.03  # |gated - ungated| may be this large, or the S1 ON drift if that is larger
RULE_ARMS = ("topk1@g0.9", "topk1@g0.96", "cband0.03@g0.9")  # the first two decide the verdict
DESIGN_GAIN = 0.03
HURT = 0.03  # gated - ungated at or below -HURT, with at least HURT_QUESTIONS worse than ungated
HURT_QUESTIONS = 3
VERDICT_TABLE = (
    "verdict, from topk1@g0.9 over S2 (T2 nexus_rdr/134-rescore-rule-2026-11-17), in this order: "
    "design changes = gain < +0.03 or wins <= losses; threshold needs work = gain in [+0.03, +0.105) or "
    "(gated - ungated <= -0.03 and >= 3 questions worse than ungated); gate helps = gain, wins/losses and "
    "worst-question tests pass and gated >= ungated + 0.03; stand = all four tests pass; otherwise "
    "'no rule case matched'. topk1@g0.96 and cband0.03@g0.9 are reported beside it and do not decide")


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def shape(d: dict[str, Any]) -> str:
    """``leg1`` (taxonomy_ab.py), ``prod`` (taxonomy_ab2.py) or ``unknown``."""
    if isinstance(d.get("sets"), dict) and isinstance(d.get("results"), list) and "summary" in d:
        return "leg1"
    if isinstance(d.get("prod"), list) and isinstance(d.get("results"), list) and "all" in (d.get("summary") or {}):
        return "prod"
    return "unknown"


def _s2_stats(results: list[dict[str, Any]], arm: str, sname: str = "S2") -> dict[str, Any]:
    """Arm against the in-run OFF, per question of ``sname``: gain, wins, losses, worst question."""
    off = [r[sname]["OFF"]["ndcg3"] for r in results]
    a = [r[sname][arm]["ndcg3"] for r in results]
    d = [x - o for x, o in zip(a, off)]
    return {"gain": _mean(a) - _mean(off), "wins": sum(x > EPS for x in d), "losses": sum(x < -EPS for x in d),
            "worst": min(d) if d else 0.0, "mean": _mean(a)}


def _core(st: dict[str, Any]) -> dict[str, bool]:
    return {"gain": st["gain"] >= GAIN_MIN - EPS,
            "wins_losses": st["wins"] >= WINS_MIN and st["losses"] <= LOSSES_MAX,
            "worst": st["worst"] >= WORST_MIN - EPS}


def _pf(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def _verdict(d: dict[str, Any]) -> str:
    """The pre-registered cases, in the registered order of precedence (T2 nexus_rdr/134-rescore-rule-2026-11-17)."""
    st, moved = d["st"], d["moved"]
    if st["gain"] < DESIGN_GAIN - EPS or st["wins"] <= st["losses"]:
        return "design changes"
    if st["gain"] < GAIN_MIN - EPS or (moved <= -HURT + EPS and d["worse"] >= HURT_QUESTIONS):
        return "threshold needs work"
    if d["core"] and moved >= HURT - EPS:
        return "gate helps"
    if d["core"] and d["stable"]:
        return "stand"
    return (f"no rule case matched: gain {st['gain']:+.4f}, wins {st['wins']}, losses {st['losses']}, "
            f"worst question {st['worst']:+.4f}, gated minus ungated {moved:+.4f} "
            f"({d['worse']} questions worse than ungated), tolerance {d['tol']:.4f}")


def rule(gated: dict[str, Any], saved: dict[str, Any] | None = None) -> list[str]:
    """The rule block: S2 within the gated run. ``topk1@g0.9`` decides (:func:`_verdict`); ``topk1@g0.96`` and
    the second arm ``cband0.03@g0.9`` are reported beside it. With the saved run, the S1 ON drift is the saved S1 arm against the same arm in this
    run; without it, the S1 delta within the run (gated arm minus ungated arm on S1)."""
    results = gated.get("results") or []
    n = len(results)
    present = [a for a in RULE_ARMS if n and a in results[0].get("S2", {})]
    if not present:
        return ["rule: this run has none of " + ", ".join(RULE_ARMS) + " (not a --gate-from run)"]
    lines = [f"rule (S2, within the gated run, n = {n} questions)"]
    decided: dict[str, Any] | None = None
    for arm in present:
        base = arm.split("@")[0]
        st = _s2_stats(results, arm)
        ung = [r["S2"][base]["ndcg3"] for r in results]
        moved = st["mean"] - _mean(ung)
        worse_than_ung = sum(r["S2"][arm]["ndcg3"] - u < -EPS for r, u in zip(results, ung))
        if saved is not None and base in (saved.get("summary", {}).get("S1") or {}) and base in results[0].get("S1", {}):
            drift = abs(_mean([r["S1"][base]["ndcg3"] for r in results]) - saved["summary"]["S1"][base]["ndcg3"])
            drift_s = f"S1 ON drift vs the saved run {drift:.4f}"
        elif arm in results[0].get("S1", {}):
            drift = abs(_mean([r["S1"][arm]["ndcg3"] for r in results]) - _mean([r["S1"][base]["ndcg3"] for r in results]))
            drift_s = f"S1 delta within the run {drift:.4f} (no saved run)"
        else:
            drift, drift_s = 0.0, "S1 drift unavailable, floor only"
        tol = max(DRIFT_FLOOR, drift)
        core = _core(st)
        stable = abs(moved) <= tol + EPS
        lines += [
            f"{arm}",
            f"  gain vs OFF {st['gain']:+.4f}; wins {st['wins']}, losses {st['losses']}; "
            f"min per-question delta vs OFF {st['worst']:+.4f}",
            f"  gated minus ungated mean {moved:+.4f} ({worse_than_ung} questions worse than ungated); "
            f"{drift_s}; tolerance max({DRIFT_FLOOR}, drift) = {tol:.4f}",
            f"  gain >= {GAIN_MIN:+.3f}: {_pf(core['gain'])}",
            f"  wins >= {WINS_MIN} and losses <= {LOSSES_MAX}: {_pf(core['wins_losses'])}",
            f"  no question worse than OFF by more than {-WORST_MIN}: {_pf(core['worst'])}",
            f"  |gated - ungated| <= max({DRIFT_FLOOR}, S1 drift): {_pf(stable)}",
            f"  => {_pf(all(core.values()) and stable)} ({arm}, "
            f"{'decides the verdict' if arm == 'topk1@g0.9' else 'reported only, does not decide'})"]
        if arm == "topk1@g0.9":
            decided = {"st": st, "moved": moved, "worse": worse_than_ung, "core": all(core.values()),
                       "stable": stable, "tol": tol}
    if decided is None:
        lines.append("verdict: not decidable (topk1@g0.9 missing)")
    else:
        lines.append("verdict: " + _verdict(decided))
    lines.append(VERDICT_TABLE)
    return lines


def prod_rule(prod: dict[str, Any]) -> list[str]:
    """Prod rule lines from a taxonomy_ab2 output: each ``@g`` top-1 arm of the summary's ``all`` block
    against OFF (gain, wins, losses from the summary; the worst question from the results) and its
    ``vs_ungated`` counts. The same four tests, the tolerance the floor (prod has no S1). No verdict."""
    summ, results = prod["summary"]["all"], prod["results"]
    arms = [a for a in summ if "@g" in a]
    if not arms:
        return ["this taxonomy_ab2 output has no @g arms (not a --gate-from run)"]
    lines = [f"prod rule (all {summ['n']} questions, within the gated run; no overall call is made for prod)"]
    for arm in arms:
        base = arm.split("@")[0]
        d = [r[arm]["ndcg3"] - r["OFF"]["ndcg3"] for r in results]
        gain = summ[arm]["ndcg3"] - summ["OFF"]["ndcg3"]
        st = {"gain": gain, "wins": summ[arm]["wins"], "losses": summ[arm]["losses"], "worst": min(d) if d else 0.0}
        core = _core(st)
        vu = summ[arm]["vs_ungated"]
        stable = abs(vu["mean_delta_ndcg3"]) <= DRIFT_FLOOR + EPS
        lines += [
            f"{arm}  (ungated arm {base}: ndcg3 {summ[base]['ndcg3']:.4f}, wins {summ[base]['wins']}, "
            f"losses {summ[base]['losses']})",
            f"  gain vs OFF {gain:+.4f}; wins {st['wins']}, losses {st['losses']}; "
            f"min per-question delta vs OFF {st['worst']:+.4f}",
            f"  gated minus ungated: better {vu['better']}, worse {vu['worse']}, same {vu['same']}; "
            f"mean {vu['mean_delta_ndcg3']:+.4f}; tolerance {DRIFT_FLOOR}",
            f"  gain {_pf(core['gain'])}; wins/losses {_pf(core['wins_losses'])}; worst question "
            f"{_pf(core['worst'])}; |gated - ungated| {_pf(stable)}; => {_pf(all(core.values()) and stable)}"]
    for note in prod.get("notes") or []:
        lines.append(f"note: {note}")
    return lines


def _header(gated: dict[str, Any]) -> list[str]:
    lines = [f"n: {gated['summary']['n']} questions, S2 {gated['summary']['S2_collections']} collections"]
    info = gated.get("gate")
    if info:
        unrouted = info["unrouted"]
        for label, names in (unrouted.items() if isinstance(unrouted, dict) else [("gate", unrouted)]):
            lines.append(f"unrouted by the gate at {label} ({len(names)}): " + ", ".join(names))
        if info.get("gate_not_in_sets"):
            lines.append("gated but not in the sets: " + ", ".join(info["gate_not_in_sets"]))
        for key in ("sets_not_in_report", "routed_not_in_report"):
            if info.get(key):
                lines.append("in the sets with no coverage row (stay routed, coverage unknown): "
                             + ", ".join(info[key]))
    return lines + [f"note: {note}" for note in gated.get("notes") or []]


def _in_run(gated: dict[str, Any], saved: dict[str, Any] | None) -> list[str]:
    lines: list[str] = []
    pr = paired(gated)
    if not pr:
        return ["", "no @g arms in this run: not a --gate-from run"]
    if pr:
        lines += ["", "paired, gated minus ungated, same run (questions)"]
        for sname, arms in pr.items():
            lines.append(f"{sname}")
            for arm, d in arms.items():
                lines.append(f"  {arm:<16} better {d['better']}, worse {d['worse']}, same {d['same']} of {d['n']}; "
                             f"mean d_ndcg3 {d['mean_d_ndcg3']:+.4f}, mean d_recall10 {d['mean_d_recall10']:+.4f}")
        lines += [""] + rule(gated, saved)
        moves = _moves(gated)
        lines += ["", f"changed questions ({len(moves)})"] + moves
    return lines


def compare(saved: dict[str, Any], gated: dict[str, Any]) -> str:
    """Saved leg-1 run against a gated run, then the gated run's own in-run sections."""
    for name, d in (("saved", saved), ("gated", gated)):
        if shape(d) != "leg1":
            return _refuse(name, d)
    bad = mismatches(saved, gated)
    if bad:
        return "\n".join(["MISMATCH: these two files are not the same measurement; no comparison printed"]
                         + [f"  {m}" for m in bad])
    return "\n".join(_header(gated) + [""] + _saved_vs_gated(saved, gated) + _in_run(gated, saved))


def paired_only(gated: dict[str, Any]) -> str:
    """One gated file, no saved run: the in-run table and rule block (S1 delta within the run)."""
    kind = shape(gated)
    if kind == "prod":
        return "\n".join(prod_rule(gated))
    if kind != "leg1":
        return _refuse("gated", gated)
    return "\n".join(_header(gated) + _in_run(gated, None))


def _refuse(name: str, d: dict[str, Any]) -> str:
    kind = shape(d)
    what = ("a taxonomy_ab2 (prod) output; pass it alone, it has no saved run to compare" if kind == "prod"
            else "neither a taxonomy_ab.py nor a taxonomy_ab2.py output")
    return f"REFUSED: the {name} file is {what}"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) not in (1, 2):
        sys.stderr.write("usage: taxonomy_ab_compare.py [SAVED.json] GATED.json   "
                         "(one file: the gated run on its own, leg 1 or prod)\n")
        return 2
    runs = [json.loads(Path(p).read_text()) for p in args]
    text = paired_only(runs[0]) if len(runs) == 1 else compare(runs[0], runs[1])
    sys.stdout.write(text + "\n")
    return 1 if text.startswith(("MISMATCH", "REFUSED")) else 0


if __name__ == "__main__":
    sys.exit(main())
