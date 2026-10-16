"""Score skill-routing runs (nexus-dg7i6.1) with the conexus/evals graders.

  python3 score_sr.py <out-dir> <conexus/evals dir> [ARM:+name | ARM:-name ...]

For every run under <out-dir>/<ARM>/<case>/r<n>/ the main-session transcript's
Skill calls are graded with that case's tool_used graders (input_match regex
over the JSON-encoded tool input; min defaults to 1, max to unbounded), the
same rule claude plugin eval applies. Per arm it reports positive-grader pass
rate (loaded-skill recall on the cases that expect a skill), the zero-load
rate on positive cases, negative-grader pass rate (absorption guards), and the
number of entries in the first request's skill listing: an arm whose listing
did not grow over S0 is vacuous, and the report says so. Arms D and N swap the
conexus tree instead of adding plugins, so for every arm the report also counts
the conexus entries and flags a duplicated listing name (two conexus copies
loaded). A marker ARM:+name says that listing name must appear in every run of
ARM, ARM:-name that it must not; it shows which conexus tree was loaded.
"""

from __future__ import annotations

import json
import re
import statistics
import sys
from pathlib import Path

LISTING_HEAD = "The following skills are available for use with the Skill tool"


def graders(case_dir: Path) -> list[dict]:
    out = []
    for g in sorted((case_dir / "graders").glob("*.md")):
        text = g.read_text(encoding="utf-8")
        front = text.split("---", 2)[1]
        fields = {}
        for line in front.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                fields[k.strip()] = v.strip().strip("'\"") if k.strip() == "input_match" else v.strip()
        if fields.get("type") != "tool_used":
            continue
        out.append({"name": g.stem, "tool": fields.get("tool", "Skill"),
                    "match": re.compile(fields["input_match"]) if "input_match" in fields else None,
                    "min": int(fields.get("min", 1)),
                    "max": int(fields["max"]) if "max" in fields else None})
    return out


def tool_calls(run: Path) -> list[tuple[str, dict]]:
    sid = json.loads((run / "ids.json").read_text())["S"]
    paths = list((run / "projects").glob(f"*/{sid}.jsonl"))
    calls = []
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("type") != "assistant" or rec.get("isSidechain"):
                continue
            for block in rec.get("message", {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    calls.append((block.get("name", ""), block.get("input") or {}))
    return calls


def listing_names(run: Path) -> list[str] | None:
    req = run / "requests.jsonl"
    if not req.exists():
        return None
    for line in req.read_text(encoding="utf-8").splitlines():
        try:
            body = json.loads(line).get("body") or {}
        except ValueError:
            continue
        blob = json.dumps(body.get("messages", []), ensure_ascii=False)
        i = blob.find(LISTING_HEAD)
        if i < 0:
            continue
        section = blob[i:].split("\\n\\n", 2)
        lines = (section[1] if len(section) > 1 else section[0]).split("\\n")
        return [ln[2:].split(": ", 1)[0] for ln in lines if ln.startswith("- ")]
    return None


def grade(calls: list[tuple[str, dict]], g: dict) -> tuple[bool, int]:
    n = sum(1 for name, inp in calls
            if name == g["tool"] and (g["match"] is None or g["match"].search(json.dumps(inp))))
    ok = n >= g["min"] and (g["max"] is None or n <= g["max"])
    return ok, n


def main() -> int:
    out_dir, evals = Path(sys.argv[1]), Path(sys.argv[2])
    markers = []
    for m in sys.argv[3:]:
        arm_name, _, rest = m.partition(":")
        markers.append((arm_name, rest[0] == "+", rest[1:]))
    problems = []
    arms = sorted(p for p in out_dir.iterdir() if p.is_dir())
    summary = {}
    for arm in arms:
        pos = neg = pos_ok = neg_ok = 0
        pos_runs = zero = 0
        listings = []
        conexus = []
        rows = []
        for case in sorted(p for p in arm.iterdir() if p.is_dir()):
            gs = graders(evals / case.name)
            for run in sorted(case.glob("r*")):
                if not (run / "ids.json").exists():
                    continue
                calls = tool_calls(run)
                skills = [i.get("skill", "?") for n, i in calls if n == "Skill"]
                if (names := listing_names(run)) is not None:
                    listings.append(len(names))
                    conexus.append(sum(1 for x in names if x.startswith("conexus:")))
                    if dups := sorted({x for x in names if names.count(x) > 1}):
                        problems.append(f"DUPLICATE LISTING NAMES {arm.name}/{case.name}/{run.name}: {dups}")
                    for m_arm, want, name in markers:
                        if m_arm == arm.name and (name in names) != want:
                            problems.append(f"MARKER FAIL {arm.name}/{case.name}/{run.name}: "
                                            f"{name} {'missing' if want else 'present'}")
                verdicts = []
                for g in gs:
                    ok, n_match = grade(calls, g)
                    negative = g["max"] == 0
                    if negative:
                        neg += 1; neg_ok += ok
                    else:
                        pos += 1; pos_ok += ok
                    verdicts.append(f"{g['name']}={'pass' if ok else 'FAIL'}({n_match})")
                if any(g["max"] != 0 for g in gs):
                    pos_runs += 1
                    zero += not skills
                rows.append(f"  {case.name:46s} {run.name} skills={skills} {' '.join(verdicts)}")
        summary[arm.name] = {
            "positive_pass": f"{pos_ok}/{pos}", "negative_pass": f"{neg_ok}/{neg}",
            "zero_load_on_positive": f"{zero}/{pos_runs}",
            "listing_entries_median": statistics.median(listings) if listings else None,
            "conexus_entries_median": statistics.median(conexus) if conexus else None}
        print(f"== {arm.name}: positive {pos_ok}/{pos}  negative {neg_ok}/{neg}  "
              f"zero-load(positive cases) {zero}/{pos_runs}  "
              f"listing entries median {summary[arm.name]['listing_entries_median']}  "
              f"conexus entries median {summary[arm.name]['conexus_entries_median']}")
        print("\n".join(rows))
    for name, s in summary.items():
        if s["listing_entries_median"] is None:
            print(f"NO LISTING FOUND for arm {name}: the listing check did not run")
    base = summary.get("S0", {}).get("listing_entries_median")
    for name, s in summary.items():
        if name in ("S1", "S2") and base is not None and s["listing_entries_median"] is not None \
                and s["listing_entries_median"] <= base:
            print(f"VACUOUS ARM {name}: listing {s['listing_entries_median']} <= S0 {base}")
    print("\n".join(problems))
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
