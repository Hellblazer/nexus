# SPDX-License-Identifier: AGPL-3.0-or-later
"""A stand-in for the `claude` CLI, for tests/e2e/plugin_out_of_repo_gate_test.sh (nexus-z7ddd).

It records what the gate handed it (argv, a fixed list of environment variables, the
working directory, the plugin's hooks file and the agents' frontmatter) to $STUB_LOG and
answers with the stream-json events a real run would produce, so the gate's isolation and
its judging can be tested with no model, no network and no real install.

$STUB_MODE
  ok          follow the instructions the way a model does (default)
  noise-once  the first run of each prompt produces no result; later runs are ok
  literal     canary runs are handed the plugin-root variable literally
  hang        spawn a long sleeper, record both pids in $STUB_PIDS and wait
$STUB_STATE   a directory for the per-prompt attempt counters
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time

ENVS = ("NX_UPGRADE_AUTO", "NEXUS_CONFIG_DIR", "NX_TOOLS_DIR", "UV_TOOL_DIR", "UV_TOOL_BIN_DIR",
        "NX_LOCKSTEP_MARKER", "NX_PLUGIN_REGISTRY", "NX_PLUGIN_MARKETPLACES", "NX_SERVICE_URL",
        "NX_NO_TELEMETRY", "HOME")
_ids = iter(range(1, 10_000))


def emit(e):
    sys.stdout.write(json.dumps(e) + "\n")


def tool_use(name, inp, parent=None):
    tid = f"t{next(_ids)}"
    emit({"type": "assistant", "parent_tool_use_id": parent,
          "message": {"content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}})
    return tid


def tool_result(tid, text, is_error=False, parent=None):
    emit({"type": "user", "parent_tool_use_id": parent,
          "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "is_error": is_error, "content": text}]}})


def read(path, text="1\tx\n", is_error=False, parent=None):
    tool_result(tool_use("Read", {"file_path": path}, parent), text, is_error, parent)


def first_line(path):
    with open(path, encoding="utf-8") as fh:
        return fh.readline().strip()


def result(text):
    emit({"type": "result", "subtype": "success", "is_error": False, "result": text})


def log_run(argv, plugin):
    with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as f:
        f.write("ARGV\t" + "\t".join(argv) + "\n")
        for v in ENVS:
            f.write(f"ENV\t{v}={os.environ.get(v, '<unset>')}\n")
        f.write(f"CWD\t{os.getcwd()}\n")
        hj = os.path.join(plugin, "hooks", "hooks.json")
        if os.path.isfile(hj):
            f.write("HOOKS\t" + open(hj, encoding="utf-8").read().strip().replace("\n", " ") + "\n")
        ad = os.path.join(plugin, "agents")
        if os.path.isdir(ad):
            for a in sorted(os.listdir(ad)):
                head = open(os.path.join(ad, a), encoding="utf-8").read().split("\n---", 1)[0]
                f.write(f"AGENT\t{a}\tmemory={'yes' if re.search(chr(10) + 'memory:', head) else 'no'}\n")


def canary(session, plugin, mode):
    lit = "${CLAUDE_PLUGIN_ROOT}"

    def target(rel):
        return os.path.join(lit if mode == "literal" else plugin, rel)

    if session == "agent":
        tool_use("Agent", {"subagent_type": "gate-canary:canary-agent", "prompt": "Follow your instructions."})
        read(target("nonces/agent.txt"), is_error=(mode == "literal"), parent="p1")
        root = plugin if os.environ.get("STUB_READ_SUBSTITUTED") else "${CLAUDE_PLUGIN_ROOT}"
        read(os.path.join(plugin, "resources/relay.md"),
             f"1\t# Canary relay\n2\tBRACED: {root}/nonces/res.txt\n3\tUNBRACED: $CLAUDE_PLUGIN_ROOT/nonces/res.txt\n", parent="p1")
        result("NONCE " + first_line(os.path.join(plugin, "nonces/agent.txt")))
    elif session == "skill-var":
        sdir = os.path.join(plugin, "skills/canary-skill-var")
        braced = lit if mode == "literal" else plugin
        emit({"type": "user", "message": {"content": [{"type": "text", "text":
              f"Base directory for this skill: {sdir}\n\nRead the file {braced}/nonces/skillvar.txt\n"
              f"BRACED: {braced}/marker\nUNBRACED: $CLAUDE_PLUGIN_ROOT/marker\n"}]}})
        read(target("nonces/skillvar.txt"), is_error=(mode == "literal"))
        result("NONCE " + first_line(os.path.join(plugin, "nonces/skillvar.txt")))
    elif session == "skill-rel":
        sdir = os.path.join(plugin, "skills/canary-skill-rel")
        emit({"type": "user", "message": {"content": [{"type": "text", "text": f"Base directory for this skill: {sdir}\n\nRead the file [nonce](./nonce.txt)\n"}]}})
        read(os.path.join(sdir, "nonce.txt"))
        result("NONCE " + first_line(os.path.join(sdir, "nonce.txt")))
    else:
        read(target("nonces/cmd.txt"), is_error=(mode == "literal"))
        result("NONCE " + first_line(os.path.join(plugin, "nonces/cmd.txt")))


def sweep(prompt, plugin):
    """An agent or skill run of the real sweep: Read every ${CLAUDE_PLUGIN_ROOT}/... path in its text."""
    m = re.search(r"subagent of type (\S+)", prompt)
    if m:
        src = os.path.join(plugin, "agents", m.group(1).split(":")[-1] + ".md")
        parent = "p1"
        tool_use("Agent", {"subagent_type": m.group(1)})
    else:
        name = re.search(r"load the skill (\S+)\.", prompt).group(1).split(":")[-1]
        src = os.path.join(plugin, "skills", name, "SKILL.md")
        parent = None
        emit({"type": "user", "message": {"content": [{"type": "text", "text":
              f"Base directory for this skill: {os.path.dirname(src)}\n"}]}})
    text = open(src, encoding="utf-8").read()
    lines = []
    for q in dict.fromkeys(re.findall(r"\$\{CLAUDE_PLUGIN_ROOT\}/[A-Za-z0-9_./\-]+", text)):
        p = q.replace("${CLAUDE_PLUGIN_ROOT}", plugin)
        if os.path.isfile(p):
            read(p, parent=parent)
        lines.append(f"REF\t{p}\t{p}")   # the text the model was handed holds the substituted path
    if parent is None:   # a skill: relative links resolve against the base directory
        for q in dict.fromkeys(re.findall(r"\]\((\./[^)]+)\)", text)):
            p = os.path.normpath(os.path.join(os.path.dirname(src), q))
            if os.path.isfile(p):
                read(p)
            lines.append(f"REF\t{q}\t{p}")
    result("\n".join(lines) if lines else "nothing to report")


def main():
    argv = sys.argv[1:]
    if "--version" in argv:
        print("9.9.9 (Claude Code stub)")
        return 0
    plugin = os.path.abspath(argv[argv.index("--plugin-dir") + 1])   # as given, symlinks unresolved, like claude
    prompt = argv[-1]
    log_run(argv, plugin)
    mode = os.environ.get("STUB_MODE", "ok")
    if mode == "hang":
        sleeper = subprocess.Popen(["sleep", "300"])
        with open(os.environ["STUB_PIDS"], "a", encoding="utf-8") as f:
            f.write(f"{os.getpid()} {sleeper.pid}\n")
        time.sleep(300)
        return 0
    key = hashlib.sha1(prompt.encode()).hexdigest()
    counter = os.path.join(os.environ["STUB_STATE"], key)
    n = int(open(counter).read()) if os.path.exists(counter) else 0
    with open(counter, "w") as f:
        f.write(str(n + 1))
    if mode == "noise-once" and n == 0:
        emit({"type": "system", "subtype": "init"})
        return 0
    emit({"type": "system", "subtype": "init"})
    if "gate-canary" in prompt:
        session = ("agent" if "canary-agent" in prompt else "skill-var" if "canary-skill-var" in prompt
                   else "skill-rel" if "canary-skill-rel" in prompt else "command")
        canary(session, plugin, mode)
    else:
        sweep(prompt, plugin)
    return 0


if __name__ == "__main__":
    sys.exit(main())
