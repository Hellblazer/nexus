# SPDX-License-Identifier: AGPL-3.0-or-later
"""The canary half of tests/e2e/plugin-out-of-repo-gate.sh (nexus-z7ddd).

The static pass (plugin_out_of_repo.py) classifies every file reference in a
plugin's text on ASSUMPTIONS about what Claude Code does with that text:

  * ${CLAUDE_PLUGIN_ROOT} is substituted in an agent's body, in a skill's
    SKILL.md and in a command's body;
  * it is NOT substituted in a file the model merely Reads (a resource, a
    skill's reference file): the model sees the literal variable;
  * a skill is announced with "Base directory for this skill: <dir>", and a
    relative link in it resolves against that directory.

The canary MEASURES them, once per gate run, on a fixture plugin the gate
builds with random nonces, so the gate does not rest on a belief about a
Claude Code version. It is plugin-independent and deterministic: the verdict
is read from the stream-json events (the tool_result of a Read, the text a
skill was delivered, the path a Read was given), never from what the model
says, and a model that fails to follow the fixture's three-line instructions
is retried (at most twice) and then reported UNVERIFIED, never as a pass.

  build   --dest DIR --manifest FILE   write the fixture plugin and its nonces
  prompts --manifest FILE              "<session><TAB><prompt>" per session
  judge   --plugin DIR --manifest FILE --events-dir DIR [--retry-list]
  isolate --src DIR --dest DIR         a scratch copy of a plugin for a live
                                       run: hooks emptied, agent memory off

Sessions (one claude -p each): agent, skill-var, skill-rel, command.
Measured, and pinned against the constants in plugin_out_of_repo.py:

  agent-body-substituted      the Read an agent made of ${CLAUDE_PLUGIN_ROOT}/...
  skill-body-substituted      the text the Skill tool delivered
  skill-unbraced-substituted  $CLAUDE_PLUGIN_ROOT (no braces) in that text
  skill-relative-resolves     a Read of ./file against the announced base dir
  command-body-substituted    the Read a slash command made
  read-file-substituted       the tool_result of a Read of a resource

Exit: 0 every session answered and every measurement matches the pins, 1 a
measurement contradicts a pin or a mechanism is broken (definite), 2 a session
produced no evidence (UNVERIFIED), 64 usage.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plugin_out_of_repo as core  # noqa: E402

PLUGIN_NAME = "gate-canary"
SESSIONS = ("agent", "skill-var", "skill-rel", "command")
NOISE = "noise"

#: What the static classifier assumes about Claude Code. The canary fails when a
#: measurement says otherwise, so a Claude Code release that changes substitution
#: turns the gate red instead of leaving the classifier wrong.
PINS = {
    "agent-body-substituted": True,
    "skill-body-substituted": True,
    "skill-unbraced-substituted": core.UNBRACED_SUBSTITUTED,
    "skill-relative-resolves": True,
    "command-body-substituted": True,
    "read-file-substituted": core.READ_FILES_SUBSTITUTED,
}


def _w(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def build(dest: str, manifest: str) -> dict:
    nonces = {k: f"CANARY-{secrets.token_hex(8)}" for k in
              ("agent", "skillvar", "skillrel", "cmd", "res")}
    _w(os.path.join(dest, ".claude-plugin", "plugin.json"),
       json.dumps({"name": PLUGIN_NAME, "version": "0.0.0",
                   "description": "Throwaway fixture built by the plugin out-of-repo gate."}) + "\n")
    for key, rel in (("agent", "nonces/agent.txt"), ("skillvar", "nonces/skillvar.txt"),
                     ("cmd", "nonces/cmd.txt"), ("res", "nonces/res.txt"),
                     ("skillrel", "skills/canary-skill-rel/nonce.txt")):
        _w(os.path.join(dest, rel), nonces[key] + "\n")
    _w(os.path.join(dest, "resources", "relay.md"),
       "# Canary relay\n"
       "BRACED: ${CLAUDE_PLUGIN_ROOT}/nonces/res.txt\n"
       "UNBRACED: $CLAUDE_PLUGIN_ROOT/nonces/res.txt\n")
    _w(os.path.join(dest, "agents", "canary-agent.md"),
       "---\nname: canary-agent\ndescription: Gate canary. Reads one nonce file.\ntools: Read\n---\n"
       "You are a test fixture. Do exactly this and nothing else.\n"
       "1. Read the file at ${CLAUDE_PLUGIN_ROOT}/nonces/agent.txt with the Read tool.\n"
       "2. Read the file at ${CLAUDE_PLUGIN_ROOT}/resources/relay.md with the Read tool.\n"
       "3. Print one line: NONCE, a space, then the first line of the file from step 1. Print nothing else.\n")
    _w(os.path.join(dest, "skills", "canary-skill-var", "SKILL.md"),
       "---\nname: canary-skill-var\ndescription: Gate canary. Reads one nonce file named with the plugin-root variable.\n---\n"
       "Read the file ${CLAUDE_PLUGIN_ROOT}/nonces/skillvar.txt with the Read tool, then print one line: "
       "NONCE, a space, then the first line of that file. Print nothing else.\n"
       "BRACED: ${CLAUDE_PLUGIN_ROOT}/marker\n"
       "UNBRACED: $CLAUDE_PLUGIN_ROOT/marker\n")
    _w(os.path.join(dest, "skills", "canary-skill-rel", "SKILL.md"),
       "---\nname: canary-skill-rel\ndescription: Gate canary. Reads one nonce file named by a relative link.\n---\n"
       "Read the file [nonce](./nonce.txt) with the Read tool. The link is relative to this skill's base "
       "directory (announced as \"Base directory for this skill\"). Then print one line: NONCE, a space, "
       "then the first line of that file. Print nothing else.\n")
    _w(os.path.join(dest, "commands", "canary-cmd.md"),
       "---\ndescription: Gate canary. Reads one nonce file.\nallowed-tools: Read\n---\n"
       "Read the file ${CLAUDE_PLUGIN_ROOT}/nonces/cmd.txt with the Read tool, then print one line: "
       "NONCE, a space, then the first line of that file. Print nothing else.\n")
    os.makedirs(os.path.dirname(os.path.abspath(manifest)), exist_ok=True)
    with open(manifest, "w", encoding="utf-8") as fh:
        json.dump({"nonces": nonces, "plugin": os.path.realpath(dest)}, fh)
    return nonces


def prompts() -> dict[str, str]:
    q = PLUGIN_NAME
    return {
        "agent": (f"Dispatch ONE subagent of type {q}:canary-agent with the Agent tool, giving it the prompt "
                  "\"Follow your instructions.\" Then print the subagent's reply verbatim and nothing else."),
        "skill-var": f"Use the Skill tool to load the skill {q}:canary-skill-var and do what it says. Use no other skill.",
        "skill-rel": f"Use the Skill tool to load the skill {q}:canary-skill-rel and do what it says. Use no other skill.",
        "command": f"/{q}:canary-cmd",
    }


# ------------------------------------------------------------------ events ---

def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


class Session:
    def __init__(self, name: str):
        self.name = name
        self.has_result = False
        self.is_error = False
        self.result = ""
        self.reads: list[tuple[str, bool, str]] = []   # (path, ok, tool_result text), in order
        self.all_text: list[str] = []                  # every text block, for the delivered skill text


def parse(path: str, name: str) -> Session:
    s = Session(name)
    pending: dict[str, str] = {}
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return s
    with fh:
        for line in fh:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("type") == "result":
                s.has_result, s.is_error, s.result = True, bool(e.get("is_error")), e.get("result") or ""
                continue
            msg = e.get("message")
            content = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content, str):
                s.all_text.append(content)
            if not isinstance(content, list):
                continue
            for b in content:
                if not isinstance(b, dict):
                    continue
                t = b.get("type")
                if t == "text":
                    s.all_text.append(b.get("text", ""))
                elif t == "tool_use" and b.get("name") == "Read":
                    pending[b["id"]] = (b.get("input") or {}).get("file_path", "")
                elif t == "tool_result":
                    body = _text_of(b.get("content"))
                    if b.get("tool_use_id") in pending:
                        s.reads.append((pending.pop(b["tool_use_id"]), not b.get("is_error"), body))
                    else:
                        s.all_text.append(body)
    return s


def _find_read(s: Session, suffix: str):
    for p, ok, body in s.reads:
        if p.endswith(suffix) or p.replace("\\", "/").endswith(suffix):
            return p, ok, body
    return None


def _literal(p: str) -> bool:
    return "CLAUDE_PLUGIN_ROOT" in p


def judge_session(name: str, s: Session, root: str, nonces: dict) -> dict:
    """-> {"state": ok|definite|noise, "detail": str, "measured": {key: bool}}."""
    out = {"session": name, "state": NOISE, "detail": "", "measured": {}}
    if not s.has_result or s.is_error:
        out["detail"] = "error result" if s.has_result else "no result event"
        return out

    def expect(rel: str, key: str, nonce_key: str, measure: str) -> None:
        want = os.path.join(root, rel)
        literal = [p for p, _ok, _b in s.reads if _literal(p)]
        got = _find_read(s, rel)
        if got and got[1]:
            out["measured"][measure] = True
            if nonces[nonce_key] in s.result:
                out["state"], out["detail"] = "ok", f"read {rel}, nonce printed"
            else:
                out["detail"] = f"read {rel} but the nonce was not printed"
        elif literal:
            out["measured"][measure] = False
            out["state"], out["detail"] = "definite", f"the model was handed the variable literally ({literal[0]})"
        elif got:
            out["detail"] = f"the Read of {want} returned an error"
        else:
            out["detail"] = f"no Read of {rel}"

    if name == "agent":
        expect("nonces/agent.txt", "agent", "agent", "agent-body-substituted")
        rel = _find_read(s, "resources/relay.md")
        if rel and rel[1]:
            lit = "${CLAUDE_PLUGIN_ROOT}" in rel[2]
            out["measured"]["read-file-substituted"] = not lit
    elif name == "skill-var":
        expect("nonces/skillvar.txt", "skillvar", "skillvar", "skill-body-substituted")
        text = "\n".join(s.all_text)
        m_b = re.search(r"^BRACED: (\S+)", text, re.M)
        m_u = re.search(r"^UNBRACED: (\S+)", text, re.M)
        if m_b:
            out["measured"]["skill-body-substituted"] = not _literal(m_b.group(1))
        if m_u:
            out["measured"]["skill-unbraced-substituted"] = not _literal(m_u.group(1))
        if not (m_b and m_u) and out["state"] == "ok":
            out["state"], out["detail"] = NOISE, "the delivered skill text is not in the event stream"
    elif name == "skill-rel":
        rel = os.path.join("skills", "canary-skill-rel", "nonce.txt")
        got = _find_read(s, "canary-skill-rel/nonce.txt")
        if got and got[1] and core.canon(got[0]) == core.canon(os.path.join(root, rel)):
            out["measured"]["skill-relative-resolves"] = True
            if nonces["skillrel"] in s.result:
                out["state"], out["detail"] = "ok", "the relative link resolved against the announced base directory"
            else:
                out["detail"] = "read the file but the nonce was not printed"
        elif got:
            out["detail"] = f"the Read of the relative link used {got[0]}"
        else:
            out["detail"] = "no Read of the relative link's target"
    elif name == "command":
        expect("nonces/cmd.txt", "cmd", "cmd", "command-body-substituted")
    return out


def judge(root: str, manifest: str, events_dir: str, retry_list: bool) -> int:
    with open(manifest, encoding="utf-8") as fh:
        nonces = json.load(fh)["nonces"]
    root = os.path.realpath(root)
    rows = []
    for n in SESSIONS:
        s = parse(os.path.join(events_dir, f"canary__{n}.events.jsonl"), n)
        rows.append(judge_session(n, s, root, nonces))
    if retry_list:
        for r in rows:
            if r["state"] == NOISE:
                print(r["session"])
        return 0
    measured: dict[str, bool] = {}
    for r in rows:
        measured.update(r["measured"])
    red = unverified = 0
    print("canary (a fixture plugin the gate built with random nonces):")
    for r in rows:
        tag = {"ok": "OPENED", "definite": "BROKEN", NOISE: "NO-EVIDENCE"}[r["state"]]
        print(f"  {tag:<12} {r['session']:<10} {r['detail']}")
        red += r["state"] == "definite"
        unverified += r["state"] == NOISE
    for key, pin in PINS.items():
        if key not in measured:
            print(f"  (not measured) {key}")
            continue
        verdict = "matches the classifier's pin" if measured[key] == pin else "CONTRADICTS the classifier's pin"
        print(f"  MEASURED {key}={'yes' if measured[key] else 'no'} ({verdict}: {'yes' if pin else 'no'})")
        red += measured[key] != pin
    if red:
        print(f"canary FAILED: {red} broken mechanism(s) or contradicted pin(s)")
        return 1
    if unverified:
        print(f"canary UNVERIFIED: {unverified} of {len(rows)} session(s) produced no evidence after the retries")
        return 2
    missing = [k for k in PINS if k not in measured]
    if missing:
        print(f"canary UNVERIFIED: not measured: {', '.join(missing)}")
        return 2
    print(f"canary clean: {len(rows)} sessions, {len(PINS)} measurements, all match the pins")
    return 0


# ----------------------------------------------------------------- isolate ---

def isolate(src: str, dest: str) -> list[str]:
    """Copy a plugin for a live run that must not touch the real install. Its hooks are
    emptied (conexus's SessionStart hooks run `nx upgrade --auto`, `nx self gc` and a version
    lockstep upgrade against the real HOME) and its agents lose `memory:` (a memory:user agent
    reads and writes ~/.claude/agent-memory). Neither is text the gate judges."""
    notes = []
    shutil.copytree(src, dest, symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__", "node_modules"))
    hj = os.path.join(dest, "hooks", "hooks.json")
    if os.path.isfile(hj):
        _w(hj, '{"hooks": {}}\n')
        notes.append("hooks/hooks.json emptied")
    pj = os.path.join(dest, ".claude-plugin", "plugin.json")
    with open(pj, encoding="utf-8") as fh:
        meta = json.load(fh)
    for key in ("hooks", "mcpServers"):
        if key in meta:
            meta.pop(key)
            notes.append(f"plugin.json {key} dropped")
    _w(pj, json.dumps(meta) + "\n")
    for dirpath, _d, files in os.walk(os.path.join(dest, "agents")):
        for f in files:
            if not f.endswith(".md"):
                continue
            p = os.path.join(dirpath, f)
            with open(p, encoding="utf-8") as fh:
                text = fh.read()
            if text.startswith("---"):
                head, sep, rest = text[3:].partition("\n---")
                new = re.sub(r"^memory:.*(?:\n|$)", "", head, flags=re.M)
                if new != head:
                    _w(p, "---" + new + sep + rest)
                    notes.append(f"{f}: memory dropped")
    for opt in (".mcp.json",):
        if os.path.exists(os.path.join(dest, opt)):
            os.remove(os.path.join(dest, opt))
            notes.append(f"{opt} removed")
    return notes


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="plugin_canary.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build"); b.add_argument("--dest", required=True); b.add_argument("--manifest", required=True)
    pr = sub.add_parser("prompts"); pr.add_argument("--manifest")
    j = sub.add_parser("judge"); j.add_argument("--plugin", required=True); j.add_argument("--manifest", required=True)
    j.add_argument("--events-dir", required=True); j.add_argument("--retry-list", action="store_true")
    i = sub.add_parser("isolate"); i.add_argument("--src", required=True); i.add_argument("--dest", required=True)
    try:
        a = p.parse_args(argv)
    except SystemExit:
        return 64
    if a.cmd == "build":
        build(a.dest, a.manifest)
        return 0
    if a.cmd == "prompts":
        for k, v in prompts().items():
            print(f"{k}\t{v}")
        return 0
    if a.cmd == "judge":
        return judge(a.plugin, a.manifest, a.events_dir, a.retry_list)
    for note in isolate(a.src, a.dest):
        print(note)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
