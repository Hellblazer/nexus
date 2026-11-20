# SPDX-License-Identifier: AGPL-3.0-or-later
"""The compaction-outcome gate's statistics and its decision rule: pure Python (math only, no scipy), no claude.

A matrix result is, per arm, a count of passes out of N runs. Three questions are asked of the counts:

  threshold      does the arm reach --threshold passes (default 11 of 15)
  separation     is the gate arm better than the control and the planted-bad arm, by a one-sided Fisher exact test at p < 0.05
                 (a threshold met by chance against a noisy control is not a result)
  calibration    does the real arm's RESUME rate (it acted on anything) look like the census's 32 of 35 real boundaries that did
                 not stall (nexus/post-compaction-resumption-census-2026-11-19, 11-17 on)? When the real arm's Wilson interval
                 lies wholly below the census rate the verdict says "scenario harsher than observed use", so that a gap on the
                 gate is not read as a gap in real sessions.

Scenario kinds (compact_outcome_scenario.py): a WORK scenario (open, meanwhile) is decided on the gate arm against control (and
bad); a GUARD scenario (bounded, waiting) asks that no hook arm do worse than control and each reach the threshold, since its pass
means "stopped" or "said what it waits on".
"""
from __future__ import annotations

import math

CENSUS_PASS, CENSUS_N = 32, 35     # boundaries since 2026-11-17 where the session did not stall (3 of 35 stalled)
ALPHA = 0.05
WORK_SCENARIOS = ("open", "meanwhile")
GUARD_SCENARIOS = ("bounded", "waiting")


def wilson(k: int, n: int, z: float = 1.959964) -> tuple[float, float]:
    """The Wilson score interval (95% by default) for k successes in n trials."""
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - half) / denom), min(1.0, (centre + half) / denom)


def fisher_one_sided(pass_a: int, n_a: int, pass_b: int, n_b: int) -> float:
    """P(arm A passes at least pass_a runs | the margins), when both arms share one pass rate: the one-sided Fisher exact test
    that A is better than B. 1.0 when the data cannot say so."""
    total, passes = n_a + n_b, pass_a + pass_b
    denom = math.comb(total, n_a)
    tail = sum(math.comb(passes, x) * math.comb(total - passes, n_a - x) for x in range(pass_a, min(passes, n_a) + 1)
               if 0 <= n_a - x <= total - passes)
    return min(1.0, tail / denom)


def calibration(real_resumed: int, real_n: int, census_pass: int = CENSUS_PASS, census_n: int = CENSUS_N) -> str:
    """One line comparing the real arm's resume rate with the census's."""
    lo, hi = wilson(real_resumed, real_n)
    c_lo, c_hi = wilson(census_pass, census_n)
    rate = census_pass / census_n
    text = (f"real arm resumed {real_resumed}/{real_n} (Wilson 95% {lo:.2f}-{hi:.2f}) vs census {census_pass}/{census_n} "
            f"({rate:.2f}, {c_lo:.2f}-{c_hi:.2f})")
    if hi < rate:
        return f"CALIBRATION: scenario harsher than observed use: {text}. Do not read a gap on this gate as a gap in real sessions."
    if lo > rate:
        return f"CALIBRATION: scenario easier than observed use: {text}."
    return f"CALIBRATION: consistent with observed use: {text}."


def arm_line(arm: str, r: dict, runs: int, min_score: float) -> str:
    lo, hi = wilson(r["pass"], runs)
    cost = r.get("cost_usd") or 0.0
    return (f"{arm:10s} {r['pass']}/{runs} pass (score >= {min_score})  Wilson 95% {lo:.2f}-{hi:.2f}  mean score {r.get('mean_score')}  "
            f"scores {r.get('scores')}  ({r['fail']} fail, {r['noise']} noise)  ${cost:.3f} (${cost / max(1, runs):.3f} a run)")


def required_arms(scenario: str, gate_arm: str, arms: list[str]) -> list[str]:
    if scenario in GUARD_SCENARIOS:
        return ["control"]
    return sorted({gate_arm, "control"} | ({"bad"} if scenario == "open" else set()))


def validate_matrix(runs: int, threshold: int, arms: list[str], gate_arm: str, scenario: str,
                    known_arms: tuple[str, ...], scenario_arms: dict[str, tuple[str, ...]]) -> "str | None":
    """A reason the matrix cannot measure anything, or None. Checked before any session starts (no money spent).

    The gate arm and the control arms of its scenario must run: without them a pass proves only that the model did the work,
    not that the hooks made the difference. The threshold must be reachable and non-zero: --runs 0 or --threshold 0 would meet
    it with no evidence at all."""
    unknown = [x for x in [*arms, gate_arm] if x not in known_arms]
    if unknown:
        return f"unknown arm(s): {unknown}"
    if gate_arm in ("bad", "control", "nocompact"):
        return f"the gate arm is a hook arm (real, directive, broad or fix), not {gate_arm}"
    if runs < 1:
        return "--runs must be at least 1"
    if not 1 <= threshold <= runs:
        return f"--threshold must be between 1 and --runs ({runs}), got {threshold}"
    bad_here = [a for a in arms if a not in scenario_arms[scenario]]
    if bad_here:
        return f"arm(s) {bad_here} do not run in the {scenario} scenario ({', '.join(scenario_arms[scenario])})"
    missing = sorted(set(required_arms(scenario, gate_arm, arms)) - set(arms))
    if scenario in GUARD_SCENARIOS and not [a for a in arms if a not in ("control", "nocompact", "bad")]:
        missing.append("a hook arm")
    if missing:
        return f"--arms for the {scenario} scenario must include {', '.join(sorted(set(missing)))}"
    return None


def decide(rates: dict, *, runs: int, threshold: int, gate_arm: str, scenario: str = "open",
           alpha: float = ALPHA) -> tuple[int, list[str]]:
    """The exit code and report lines for one scenario from per-arm counts ``{arm: {pass, fail, noise, resumed?}}``.

    2 UNVERIFIED: any run ended NOISE or UNVERIFIED after its retry (never a pass), or a required arm has no counts.
    1: the gate arm is under the threshold, or not separable from the control (and bad) arm by a one-sided Fisher exact test at
    *alpha*, or the bad or control arm reached the threshold (the gate does not discriminate); in a guard scenario, a hook arm
    under the threshold or worse than control. 0 otherwise. A CALIBRATION line is added when the real arm ran."""
    noisy = {arm: r["noise"] for arm, r in rates.items() if r["noise"]}
    if noisy:
        detail = ", ".join(f"{arm} {n}" for arm, n in sorted(noisy.items()))
        return 2, [f"UNVERIFIED: runs without a verdict ({detail}): no automatic compaction, a turn that did not finish, "
                   "hooks that did not run, a precondition of the scenario that failed, or a stale stub nx. No result is reported."]
    needed = required_arms(scenario, gate_arm, list(rates))
    missing = [a for a in needed if a not in rates]
    if missing:
        return 2, [f"UNVERIFIED: arm(s) not run: {', '.join(missing)}"]
    lines: list[str] = []
    code = 0
    if scenario in GUARD_SCENARIOS:
        guarded = [a for a in rates if a not in ("control", "nocompact", "bad")]
        if not guarded:
            return 2, ["UNVERIFIED: no hook arm ran"]
        ctl = rates["control"]
        for arm in sorted(guarded):
            r = rates[arm]
            p_worse = fisher_one_sided(ctl["pass"], runs, r["pass"], runs)
            ok = r["pass"] >= threshold and p_worse >= alpha
            lines.append(f"{scenario}: {arm} {r['pass']}/{runs} vs threshold {threshold}, control {ctl['pass']}/{runs} "
                         f"(p {p_worse:.3f} that control is better): {'PASS' if ok else 'FAIL'}")
            if not ok:
                code = 1
                lines.append(f"GUARD FAILED: the {arm} arm {'is below the threshold' if r['pass'] < threshold else 'does worse than control'} "
                             f"in the {scenario} scenario")
    else:
        gated = rates[gate_arm]
        reached = gated["pass"] >= threshold
        lines.append(f"{scenario}: {gate_arm} arm {gated['pass']}/{runs} vs threshold {threshold}: {'PASS' if reached else 'FAIL'}")
        code = 0 if reached else 1
        for name in ("bad", "control"):
            if name not in rates:
                continue
            if rates[name]["pass"] >= threshold:
                lines.append(f"NON-VACUITY FAILED: the {name} arm reached the threshold; the gate does not discriminate on this seed")
                code = 1
                continue
            p = fisher_one_sided(gated["pass"], runs, rates[name]["pass"], runs)
            lines.append(f"{gate_arm} vs {name}: one-sided Fisher p = {p:.4f} ({'separable' if p < alpha else 'NOT SEPARABLE'} at {alpha})")
            if p >= alpha:
                code = 1
        if code and reached:
            lines.append("FAILED: the threshold is met but the gate arm is not separable from the control arms")
    if "real" in rates and scenario in WORK_SCENARIOS:
        lines.append(calibration(rates["real"].get("resumed", 0), runs))
    if "nocompact" in rates:
        n = rates["nocompact"]
        lines.append(f"nocompact baseline (no compaction, same turns): {n['pass']}/{runs} pass. A session that never compacts scores "
                     "this; read the other arms against it.")
    return code, lines
