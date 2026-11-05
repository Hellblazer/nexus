# SPDX-License-Identifier: AGPL-3.0-or-later
"""The canary half of tests/e2e/plugin-out-of-repo-gate.sh (nexus-z7ddd).

The static pass (plugin_out_of_repo.py) classifies every file reference in a
plugin's text on ASSUMPTIONS about what Claude Code does with that text:

  * ${CLAUDE_PLUGIN_ROOT} is substituted in an agent's body, in a skill's
    SKILL.md and in a command's body (and in an agent's frontmatter description);
  * it is NOT substituted in a file the model merely Reads (a resource, a
    skill's reference file): the model sees the literal variable;
  * the unbraced $CLAUDE_PLUGIN_ROOT is not substituted anywhere;
  * a skill is announced with "Base directory for this skill: <dir>", and a
    relative link in it resolves against that directory;
  * a relative link inside a file the model Reads resolves against that file's
    directory.

The canary MEASURES them, once per gate run, on a fixture plugin the gate
builds with random nonces, so the gate does not rest on a belief about a
Claude Code version. It is plugin-independent: the verdict is read from the
stream-json events (the tool_result of a Read, the text a skill was delivered,
the path a Read was given, the init event), never from what the model says,
beyond one thing: a session also has to print its nonce (the model's output,
the only place the gate reads it). A model that skips a step of the fixture's
short instructions leaves a measurement missing: the session is NOISE, retried
(at most twice), and then reported UNVERIFIED, never as a pass.

  build   --dest DIR --manifest FILE   write the fixture plugin and its nonces
  prompts --manifest FILE              "<session><TAB><prompt>" per session
  judge   --plugin DIR --manifest FILE --events-dir DIR [--retry-list]
  isolate --src DIR --dest DIR         a scratch copy of a plugin for a live
                                       run: hooks emptied, agent memory off,
                                       agents narrowed to the Read tool

Sessions (one claude -p each): agent, agent-ub, skill-var, skill-rel, command.
Measured, and pinned against the constants in plugin_out_of_repo.py:

  agent-body-substituted        the Read an agent made of ${CLAUDE_PLUGIN_ROOT}/...
  agent-unbraced-substituted    the Read an agent made of $CLAUDE_PLUGIN_ROOT/...
  agent-description-substituted the Read the parent made of the path its Agent tool listing
                                gave in an agent's frontmatter description
  read-file-substituted         the tool_result of a Read of a resource
  resource-relative-resolves    the Read made of a relative link inside that resource
  skill-body-substituted        the text the Skill tool delivered
  skill-unbraced-substituted    $CLAUDE_PLUGIN_ROOT (no braces) in that text
  skill-reference-file-substituted  the tool_result of a Read of a skill's reference.md
  skill-base-dir-announced      "Base directory for this skill: <the skill's dir>" in the text
  skill-relative-resolves       a Read of ./file against the announced base dir
  command-body-substituted      the Read a slash command made
  command-unbraced-substituted  the Read a slash command made of the unbraced variable

Every session's init event is judged too (plugin_out_of_repo.check_init): a plugin
other than the fixture, an MCP server, another tool set or a non-default
permission mode is a definite red.

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
SESSIONS = ("agent", "agent-ub", "skill-var", "skill-rel", "command")
NOISE = "noise"
BASEDIR_LINE = re.compile(r"^Base directory for this skill: (/\S.*)$", re.M)
SKILL_REL_BODY = "[nonce](./nonce.txt)"   # the text of canary-skill-rel that proves its body was delivered
LIT = "${CLAUDE_PLUGIN_ROOT}"

#: What the static classifier assumes about Claude Code. The canary fails when a
#: measurement says otherwise, so a Claude Code release that changes substitution
#: turns the gate red instead of leaving the classifier wrong.
PINS = {
    "agent-body-substituted": True,
    "agent-unbraced-substituted": core.UNBRACED_SUBSTITUTED,
    "agent-description-substituted": core.FRONTMATTER_SUBSTITUTED,
    "read-file-substituted": core.READ_FILES_SUBSTITUTED,
    "resource-relative-resolves": True,
    "skill-body-substituted": True,
    "skill-unbraced-substituted": core.UNBRACED_SUBSTITUTED,
    "skill-reference-file-substituted": core.READ_FILES_SUBSTITUTED,
    "skill-base-dir-announced": True,
    "skill-relative-resolves": True,
    "command-body-substituted": True,
    "command-unbraced-substituted": core.UNBRACED_SUBSTITUTED,
}


def _w(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


NONCE_KEYS = ("agent", "agentub", "fm", "skillvar", "skillrel", "cmd", "cmdub", "res", "sib")


def build(dest: str, manifest: str) -> dict:
    nonces = {k: f"CANARY-{secrets.token_hex(8)}" for k in NONCE_KEYS}
    _w(os.path.join(dest, ".claude-plugin", "plugin.json"),
       json.dumps({"name": PLUGIN_NAME, "version": "0.0.0",
                   "description": "Throwaway fixture built by the plugin out-of-repo gate."}) + "\n")
    for key, rel in (("agent", "nonces/agent.txt"), ("agentub", "nonces/agentub.txt"), ("fm", "nonces/fm.txt"),
                     ("skillvar", "nonces/skillvar.txt"), ("cmd", "nonces/cmd.txt"), ("cmdub", "nonces/cmdub.txt"),
                     ("res", "nonces/res.txt"), ("sib", "resources/sibling.txt"),
                     ("skillrel", "skills/canary-skill-rel/nonce.txt")):
        _w(os.path.join(dest, rel), nonces[key] + "\n")
    _w(os.path.join(dest, "resources", "relay.md"),
       "# Canary relay\n"
       "BRACED: ${CLAUDE_PLUGIN_ROOT}/nonces/res.txt\n"
       "UNBRACED: $CLAUDE_PLUGIN_ROOT/nonces/res.txt\n"
       "See the [sibling](./sibling.txt) file.\n")
    _w(os.path.join(dest, "agents", "canary-agent.md"),
       "---\nname: canary-agent\ndescription: Gate canary. Reads three files.\ntools: Read\n---\n"
       "You are a test fixture. Do exactly this and nothing else.\n"
       "1. Read the file at ${CLAUDE_PLUGIN_ROOT}/nonces/agent.txt with the Read tool.\n"
       "2. Read the file at ${CLAUDE_PLUGIN_ROOT}/resources/relay.md with the Read tool.\n"
       "3. Follow the link named sibling in the file from step 2, reading its target with the Read tool.\n"
       "4. Print one line: NONCE, a space, then the first line of the file from step 1. Print nothing else.\n")
    _w(os.path.join(dest, "agents", "canary-agent-ub.md"),
       "---\nname: canary-agent-ub\n"
       "description: Gate canary. Its marker file is ${CLAUDE_PLUGIN_ROOT}/nonces/fm.txt\n"
       "tools: Read\n---\n"
       "You are a test fixture. Do exactly this and nothing else.\n"
       "1. Read the file at $CLAUDE_PLUGIN_ROOT/nonces/agentub.txt with the Read tool. If the Read fails, go on.\n"
       "2. Print one line: NONCE, a space, then the first line of that file, or NONE if the Read failed. "
       "Print nothing else.\n")
    _w(os.path.join(dest, "skills", "canary-skill-var", "SKILL.md"),
       "---\nname: canary-skill-var\ndescription: Gate canary. Reads two files named with the plugin-root variable.\n---\n"
       "1. Read the file ${CLAUDE_PLUGIN_ROOT}/nonces/skillvar.txt with the Read tool.\n"
       "2. Read the file ${CLAUDE_PLUGIN_ROOT}/skills/canary-skill-var/reference.md with the Read tool.\n"
       "3. Print one line: NONCE, a space, then the first line of the file from step 1. Print nothing else.\n"
       "BRACED: ${CLAUDE_PLUGIN_ROOT}/marker\n"
       "UNBRACED: $CLAUDE_PLUGIN_ROOT/marker\n")
    _w(os.path.join(dest, "skills", "canary-skill-var", "reference.md"),
       "# Canary skill reference\n"
       "BRACED: ${CLAUDE_PLUGIN_ROOT}/nonces/res.txt\n")
    _w(os.path.join(dest, "skills", "canary-skill-rel", "SKILL.md"),
       "---\nname: canary-skill-rel\ndescription: Gate canary. Reads one nonce file named by a relative link.\n---\n"
       f"Read the file {SKILL_REL_BODY} with the Read tool. The link is relative to this skill's base "
       "directory (announced as \"Base directory for this skill\"). Then print one line: NONCE, a space, "
       "then the first line of that file. Print nothing else.\n")
    _w(os.path.join(dest, "commands", "canary-cmd.md"),
       "---\ndescription: Gate canary. Reads two files.\nallowed-tools: Read\n---\n"
       "1. Read the file ${CLAUDE_PLUGIN_ROOT}/nonces/cmd.txt with the Read tool.\n"
       "2. Read the file $CLAUDE_PLUGIN_ROOT/nonces/cmdub.txt with the Read tool. If the Read fails, go on.\n"
       "3. Print one line: NONCE, a space, then the first line of the file from step 1. Print nothing else.\n")
    os.makedirs(os.path.dirname(os.path.abspath(manifest)), exist_ok=True)
    with open(manifest, "w", encoding="utf-8") as fh:
        json.dump({"nonces": nonces, "plugin": os.path.realpath(dest)}, fh)
    return nonces


def prompts() -> dict[str, str]:
    q = PLUGIN_NAME
    return {
        "agent": (f"Dispatch ONE subagent of type {q}:canary-agent with the Agent tool, giving it the prompt "
                  "\"Follow your instructions.\" Then print the subagent's reply verbatim and nothing else."),
        "agent-ub": (f"The subagent type {q}:canary-agent-ub has a marker file whose path is written in its "
                     "description in your list of subagent types. First Read that file, using the path exactly as "
                     f"written there; if that Read fails, carry on regardless. Then dispatch ONE subagent of type {q}:canary-agent-ub with the Agent tool, "
                     "giving it the prompt \"Follow your instructions.\" Then print the subagent's reply verbatim "
                     "and nothing else."),
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


def _literal(p: str) -> bool:
    return "CLAUDE_PLUGIN_ROOT" in p


def probe(s: Session, root: str, rel: str) -> tuple[str, str, str]:
    """What the session did about the file `rel` of the fixture -> (status, path, tool_result text).

    ok       a Read of exactly root/rel returned the file
    error    a Read of exactly root/rel returned an error
    literal  a Read was given a path that still holds the variable and ends in rel
    elsewhere  a Read of a file with rel's name, from another directory
    none     no Read of it at all"""
    want = core.canon(os.path.join(root, rel))
    norm = rel.replace("\\", "/")
    statuses = {"ok": None, "error": None, "literal": None, "elsewhere": None}
    for p, ok, body in s.reads:
        q = p.replace("\\", "/")
        if _literal(p):
            if q.endswith("/" + norm) and statuses["literal"] is None:
                statuses["literal"] = (p, body)
        elif core.canon(p) == want:
            key = "ok" if ok else "error"
            if statuses[key] is None:
                statuses[key] = (p, body)
        elif os.path.basename(q) == os.path.basename(norm) and statuses["elsewhere"] is None:
            statuses["elsewhere"] = (p, body)
    for key in ("ok", "literal", "error", "elsewhere"):
        if statuses[key] is not None:
            return (key, *statuses[key])
    return "none", "", ""


def judge_session(name: str, s: Session, root: str, nonces: dict, events_path: str = "") -> dict:
    """-> {"state": ok|definite|noise, "detail": str, "measured": {key: bool}}.

    definite: a mechanism the classifier relies on is broken, or the run was not isolated.
    noise: a step of the fixture's instructions was not carried out, so a measurement is missing."""
    out = {"session": name, "state": NOISE, "detail": "", "measured": {}}
    if not s.has_result or s.is_error:
        out["detail"] = "error result" if s.has_result else "no result event"
        return out
    meas: dict[str, bool] = out["measured"]
    gaps: list[str] = []     # a missing measurement: noise
    broken: list[str] = []   # a mechanism that does not work: definite

    if events_path:
        for why in core.check_init(events_path, PLUGIN_NAME):
            broken.append(f"isolation: {why}")

    def read(rel: str, key: str, *, braced: bool = False) -> tuple[str, str]:
        """Record whether `rel`'s path reached the model substituted; a braced path that did not is broken."""
        st, path, body = probe(s, root, rel)
        if st == "ok":
            meas[key] = True
        elif st == "literal":
            meas[key] = False
            if braced:
                broken.append(f"the model was handed the variable literally ({path})")
        elif st == "elsewhere":
            gaps.append(f"{rel} was Read from another directory ({path})")
        elif st == "error":
            gaps.append(f"the Read of {rel} returned an error")
        else:
            gaps.append(f"{rel} was not Read")
        return st, body

    def printed(nonce_key: str, rel: str) -> None:
        if nonces[nonce_key] not in s.result:
            gaps.append(f"read {rel} but the nonce was not printed")

    text = "\n".join(s.all_text)
    if name == "agent":
        read("nonces/agent.txt", "agent-body-substituted", braced=True)
        st, body = probe(s, root, "resources/relay.md")[0::2]
        if st == "ok":
            meas["read-file-substituted"] = LIT not in body
        else:
            gaps.append("relay.md was not Read" if st == "none" else "the Read of relay.md did not return the file")
        # a relative link inside a file the model Reads: the model must read the target, and where it reads decides
        st, path, _b = probe(s, root, "resources/sibling.txt")
        if st == "ok":
            meas["resource-relative-resolves"] = True
        elif st == "elsewhere":
            meas["resource-relative-resolves"] = False
        elif st == "error":
            gaps.append("the Read of the sibling file returned an error")
        elif st != "literal":
            gaps.append("the link in relay.md was not followed")
        printed("agent", "agent.txt")
    elif name == "agent-ub":
        read("nonces/fm.txt", "agent-description-substituted")
        read("nonces/agentub.txt", "agent-unbraced-substituted")
    elif name == "skill-var":
        read("nonces/skillvar.txt", "skill-body-substituted", braced=True)
        st, body = probe(s, root, "skills/canary-skill-var/reference.md")[0::2]
        if st == "ok":
            meas["skill-reference-file-substituted"] = LIT not in body
        else:
            gaps.append("the skill's reference.md was not Read" if st == "none"
                        else "the Read of the skill's reference.md did not return the file")
        m_b = re.search(r"^BRACED: (\S+)", text, re.M)
        m_u = re.search(r"^UNBRACED: (\S+)", text, re.M)
        if m_b:
            meas["skill-body-substituted"] = not _literal(m_b.group(1))
        if m_u:
            meas["skill-unbraced-substituted"] = not _literal(m_u.group(1))
        if not (m_b and m_u):
            gaps.append("the delivered skill text is not in the event stream")
        else:
            _announced(text, root, "canary-skill-var", meas)
        printed("skillvar", "skillvar.txt")
    elif name == "skill-rel":
        if SKILL_REL_BODY not in text:
            gaps.append("the delivered skill text is not in the event stream")
        else:
            _announced(text, root, "canary-skill-rel", meas)
        st, path, _b = probe(s, root, "skills/canary-skill-rel/nonce.txt")
        if st == "ok":
            meas["skill-relative-resolves"] = True
            printed("skillrel", "nonce.txt")
        elif st == "elsewhere":
            meas["skill-relative-resolves"] = False
            broken.append(f"the relative link did not resolve against the announced base directory (Read {path})")
        elif st == "error":
            gaps.append("the Read of the relative link's target returned an error")
        else:
            gaps.append("no Read of the relative link's target")
    elif name == "command":
        read("nonces/cmd.txt", "command-body-substituted", braced=True)
        read("nonces/cmdub.txt", "command-unbraced-substituted")
        printed("cmd", "cmd.txt")

    if broken:
        out["state"], out["detail"] = "definite", "; ".join(broken)
    elif gaps:
        out["detail"] = "; ".join(gaps)
    else:
        out["state"], out["detail"] = "ok", "every step carried out; " + ", ".join(sorted(meas))
    return out


def _announced(text: str, root: str, skill: str, meas: dict) -> None:
    """The announcement is part of the contract the static pass relies on: assert it, do not assume it."""
    want = core.canon(os.path.join(root, "skills", skill))
    dirs = [core.canon(m.strip()) for m in BASEDIR_LINE.findall(text)]
    ok = want in dirs
    meas["skill-base-dir-announced"] = meas.get("skill-base-dir-announced", True) and ok


def judge(root: str, manifest: str, events_dir: str, retry_list: bool) -> int:
    with open(manifest, encoding="utf-8") as fh:
        nonces = json.load(fh)["nonces"]
    root = os.path.realpath(root)
    rows = []
    for n in SESSIONS:
        path = os.path.join(events_dir, f"canary__{n}.events.jsonl")
        rows.append(judge_session(n, parse(path, n), root, nonces, path))
    if retry_list:
        for r in rows:
            if r["state"] == NOISE:
                print(r["session"])
        return 0
    measured: dict[str, bool] = {}
    for r in rows:
        for k, v in r["measured"].items():
            measured[k] = measured.get(k, True) and v
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

#: Agent frontmatter keys a live run must not honour: memory reads and writes the real
#: ~/.claude/agent-memory; the others widen what the subagent can reach beyond Read.
DROPPED_AGENT_KEYS = ("memory", "disallowedTools", "mcpServers", "hooks")


def narrow_frontmatter(head: str) -> str:
    """The frontmatter of an agent (the text between the `---` lines) for a live run: `tools: Read`
    and none of DROPPED_AGENT_KEYS (continuation lines included). `--allowedTools` only auto-approves;
    the tools a subagent HAS come from its own frontmatter, so the narrowing has to be here."""
    keep: list[str] = []
    skipping = False
    for line in head.split("\n"):
        key = re.match(r"^([A-Za-z][\w-]*):", line)
        if key:
            skipping = key.group(1) in DROPPED_AGENT_KEYS or key.group(1) == "tools"
            if skipping:
                continue
        elif skipping and (line.startswith((" ", "\t", "-")) or not line.strip()):
            continue
        else:
            skipping = False
        keep.append(line)
    while keep and not keep[-1].strip():
        keep.pop()
    keep.append("tools: Read")
    return "\n".join(keep)


def isolate(src: str, dest: str) -> list[str]:
    """Copy a plugin for a live run that must not touch the real install. Its hooks are
    emptied (conexus's SessionStart hooks run `nx upgrade --auto`, `nx self gc` and a version
    lockstep upgrade against the real HOME), its agents lose `memory:` (a memory:user agent
    reads and writes ~/.claude/agent-memory) and are narrowed to the Read tool. None of it is
    text the gate judges."""
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
                new = narrow_frontmatter(head)
                if new != head:
                    _w(p, "---" + new + sep + rest)
                    notes.append(f"{f}: narrowed to the Read tool (memory, disallowedTools, mcpServers, hooks dropped)")
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
