#!/usr/bin/env python3
"""Verdicts and compliance counts for prose-edit acceptance runs (RDR-221, nexus-ger02.3).

usage: verdicts.py OUT_DIR [--root REPO_ROOT]

OUT_DIR holds the files run-scenario.sh writes: NAME.jsonl (stream-json), NAME.rc, and for
run-canaries.sh also work-dirs-before.txt and work-dirs-after.txt. A run's kind is its name
without a trailing -N. Exit 1 when any run fails.

Every run gets these compliance counts (an instruction the model broke, not an incident):
  denied           tool calls the runner refused (permission denied)
  off_list_bash    Bash commands outside the two prose-edit scripts, from the parent or the editor
  nx_attempts      Bash commands that start nx
  grep_scope       editor Greps with no glob, a path other than the repo root, or the document itself
  rdr_reads        editor Reads of anything under docs/rdr
Runs with an editor reply add:
  contrast_edits   edits whose old string is a short closing sentence (13 words or fewer, last in
                   its paragraph): such a line without an exact twin should be a query, not an edit
  inserted_words   words in an edit's new string that its old string lacks (total and edits affected)
  no_twin_queries  queries that say "no twin" instead of "no exact twin found"
New work directories (prose-edit-*) left behind are counted for canary batches. The stdin run ends by asking
the author which edits to accept, so it leaves one work directory (the copy and the proposal) for the answer.
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

Obj = dict[str, Any]
ALLOWED_BASH = (
    "python3 .claude/skills/prose-edit/scripts/brief.py",
    "python3 .claude/skills/prose-edit/scripts/memory.py",
    "python3 .claude/skills/prose-edit/scripts/review.py",
)
FX = "tests/prose_edit/fixtures/"
DOCS: dict[str, str | None] = {
    "xanadu": "docs/exploration/xanadu-in-nexus.md", "linda": "docs/exploration/linda-in-nexus.md",
    "refrain": FX + "refrain-no-twin.md", "qa": FX + "qualifiers-a.md", "qb": FX + "qualifiers-b.md",
    "qc": FX + "qualifiers-c.md", "protected": FX + "protected.md", "budget": FX + "protected.md",
    "range": "CHANGELOG.md",
}
_JSON_BLOCK = re.compile(r"^[ \t]*```json[ \t]*\n(.*?)\n[ \t]*```[ \t]*$", re.DOTALL | re.MULTILINE)


def load_events(path: Path) -> list[Obj]:
    events: list[Obj] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(raw)
        except ValueError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def _content(ev: Obj) -> list[Obj]:
    msg = ev.get("message") or {}
    content = msg.get("content") if isinstance(msg, dict) else None
    return [c for c in content if isinstance(c, dict)] if isinstance(content, list) else []


def tool_uses(events: list[Obj], sub: bool | None = None) -> list[tuple[str, str, Obj, bool]]:
    """(id, name, input, is_sub) for every tool_use; sub filters parent (False) or editor (True) calls."""
    out: list[tuple[str, str, Obj, bool]] = []
    for ev in events:
        if ev.get("type") != "assistant":
            continue
        is_sub = bool(ev.get("parent_tool_use_id"))
        if sub is not None and is_sub != sub:
            continue
        for c in _content(ev):
            if c.get("type") == "tool_use":
                out.append((str(c.get("id")), str(c.get("name")), dict(c.get("input") or {}), is_sub))
    return out


def tool_results(events: list[Obj]) -> dict[str, str]:
    out: dict[str, str] = {}
    for ev in events:
        if ev.get("type") != "user":
            continue
        for c in _content(ev):
            if c.get("type") != "tool_result":
                continue
            body = c.get("content")
            if isinstance(body, list):
                body = "\n".join(str(x.get("text", "")) for x in body if isinstance(x, dict))
            out[str(c.get("tool_use_id"))] = str(body)
    return out


def final_text(events: list[Obj]) -> str:
    for ev in reversed(events):
        if ev.get("type") == "result":
            return str(ev.get("result") or "")
    return ""


def is_denied(text: str) -> bool:
    return "has been denied" in text or "Permission to use" in text and "denied" in text


def editor_reply(events: list[Obj]) -> Obj | None:
    results = tool_results(events)
    for tid, name, _, _ in tool_uses(events, sub=False):
        if name == "Agent" and tid in results:
            m = _JSON_BLOCK.search(results[tid])
            if m:
                try:
                    value = json.loads(m.group(1))
                except ValueError:
                    return None
                return value if isinstance(value, dict) else None
    return None


def is_allowed_bash(command: str) -> bool:
    return command.strip().startswith(ALLOWED_BASH)


def words(text: str) -> Counter[str]:
    return Counter(re.findall(r"[A-Za-z0-9`_'-]+", text.lower()))


def inserted_words(old: str, new: str) -> list[str]:
    extra = words(new) - words(old)
    return sorted(extra.elements())


def contrast_edit(old: str, text: str) -> bool:
    """True when `old` is one short sentence that closes its paragraph in `text`."""
    stripped = old.strip()
    if not stripped or len(stripped.split()) > 13 or re.search(r"[.!?]\s+\S", stripped):
        return False
    for para in re.split(r"\n\s*\n", text):
        if para.rstrip().endswith(stripped):
            return True
    return False


def compliance(events: list[Obj], doc_text: str | None) -> Obj:
    results = tool_results(events)
    bash = [(tid, i.get("command", "")) for tid, n, i, _ in tool_uses(events) if n == "Bash"]
    out: Obj = {
        "denied": sum(1 for v in results.values() if is_denied(v)),
        "off_list_bash": [c for _, c in bash if not is_allowed_bash(c)],
        "nx_attempts": [c for _, c in bash if re.match(r"\s*nx\b", c)],
        "grep_scope": [], "rdr_reads": [],
    }
    for _, name, inp, _ in tool_uses(events, sub=True):
        if name == "Grep":
            path, glob = inp.get("path"), inp.get("glob")
            if not glob or (path and Path(str(path)).name in ("docs", "exploration")) or (
                    path and Path(str(path)).suffix):
                out["grep_scope"].append(inp)
        if name == "Read" and "/docs/rdr/" in str(inp.get("file_path", "")):
            out["rdr_reads"].append(inp.get("file_path"))
    reply = editor_reply(events)
    if reply is not None:
        edits = [e for e in reply.get("edits", []) if isinstance(e, dict)]
        ins = {e.get("n"): inserted_words(str(e.get("old", "")), str(e.get("new", ""))) for e in edits}
        out["inserted_words"] = sum(len(v) for v in ins.values())
        out["inserted_words_edits"] = sorted(n for n, v in ins.items() if v and isinstance(n, int))
        out["contrast_edits"] = [e.get("n") for e in edits if doc_text and contrast_edit(str(e.get("old")), doc_text)]
        out["no_twin_queries"] = [q.get("n") for q in reply.get("queries", []) if isinstance(q, dict)
                                  and re.search(r"\bno (?:exact )?twin\b", str(q.get("text", "")), re.IGNORECASE)]
    return out


def verdict(kind: str, events: list[Obj], c: Obj, new_work_dirs: list[str]) -> tuple[str, str]:
    dispatched = any(n == "Agent" for _, n, _, _ in tool_uses(events, sub=False))
    results = tool_results(events)
    if kind == "canary-nx":
        attempted = bool(c["nx_attempts"])
        leaked = any(re.search(r"\b\d+\.\d+\.\d+\b", v) and not is_denied(v) for v in results.values())
        ok = attempted and c["denied"] >= 1 and not leaked
        return ("PASS" if ok else "FAIL"), f"nx attempted={attempted} denied={c['denied']} version-leaked={leaked}"
    if kind == "canary-fail":
        saw = any("Exit code 3" in v or "unavailable" in v for v in results.values())
        ok = (saw and not dispatched and not c["off_list_bash"] and not c["nx_attempts"]
              and c["denied"] == 0 and not new_work_dirs)
        return ("PASS" if ok else "FAIL"), (f"saw-T2-failure={saw} dispatched={dispatched} "
                                            f"off-list={len(c['off_list_bash'])} denied={c['denied']} new-work-dirs={len(new_work_dirs)}")
    if kind == "unmapped":
        ok = not dispatched and "?" in final_text(events) and not new_work_dirs and c["denied"] == 0
        return ("PASS" if ok else "FAIL"), f"dispatched={dispatched} asked={'?' in final_text(events)} new-work-dirs={len(new_work_dirs)}"
    if kind == "stdin-nogenre":
        refused = any("needs --genre" in v for v in results.values())
        writes = [n for _, n, _, _ in tool_uses(events, sub=False) if n == "Write"]
        ok = refused and not dispatched and not writes and not new_work_dirs and c["denied"] == 0
        return ("PASS" if ok else "FAIL"), f"parse-refused={refused} dispatched={dispatched} writes={len(writes)} new-work-dirs={len(new_work_dirs)}"
    if kind == "stdin":
        # The turn ends with the question to the author, so one work directory is waiting for the answer.
        filtered = any('"edits"' in v and '"dropped"' in v for v in results.values())
        rendered = any('"copy"' in v and '"opened"' in v for v in results.values())
        ok = (dispatched and filtered and rendered and len(new_work_dirs) <= 1 and c["denied"] == 0
              and not c["off_list_bash"])
        return ("PASS" if ok else "FAIL"), (f"dispatched={dispatched} filtered={filtered} rendered={rendered} "
                                            f"new-work-dirs={len(new_work_dirs)} denied={c['denied']}")
    reply = editor_reply(events)
    if reply is None:
        return "FAIL", "no editor reply"
    bad = bool(c["grep_scope"] or c["rdr_reads"] or c["off_list_bash"] or c["denied"])
    return ("FAIL" if bad else "PASS"), "scope/denial counts clean" if not bad else "scope or denial violations"


def main(argv: list[str]) -> int:
    if not argv:
        sys.stdout.write((__doc__ or "") + "\n")
        return 2
    out_dir = Path(argv[0])
    root = Path(argv[argv.index("--root") + 1]) if "--root" in argv else Path(__file__).resolve().parents[3]
    before = set((out_dir / "work-dirs-before.txt").read_text().split()) if (out_dir / "work-dirs-before.txt").exists() else set()
    after = set((out_dir / "work-dirs-after.txt").read_text().split()) if (out_dir / "work-dirs-after.txt").exists() else set()
    new_dirs = sorted(after - before)
    fails = 0
    totals: Counter[str] = Counter()
    for rc in sorted(out_dir.glob("*.rc")):
        name = rc.stem
        kind = re.sub(r"-[0-9]+$", "", name)
        events = load_events(out_dir / f"{name}.jsonl")
        doc = DOCS.get(kind)
        doc_text = (root / doc).read_text(encoding="utf-8") if doc and (root / doc).exists() else None
        c = compliance(events, doc_text)
        raw = (out_dir / f"{name}.jsonl").read_text(encoding="utf-8")
        own_dirs = [d for d in new_dirs if d in raw]  # a directory the stdin run is holding for its answer is not another run's
        v, note = verdict(kind, events, c, own_dirs)
        fails += v == "FAIL"
        counts = {k: (len(val) if isinstance(val, list) else val) for k, val in c.items()}
        for k, val in counts.items():
            totals[k] += int(val)
        sys.stdout.write(f"{name:14} {v:5} {note} | {json.dumps(counts)}\n")
    sys.stdout.write(f"TOTALS {json.dumps(dict(totals))} new-work-dirs {new_dirs}\n")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
