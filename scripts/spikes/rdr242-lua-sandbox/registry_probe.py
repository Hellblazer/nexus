# ruff: noqa: T201, PLC0415  (a probe prints its results; imports kept local to their case)
"""RDR-242 registry-shape probe: register, check, trial, approve, run, revoke.

The registry is an in-memory stand-in for the engine table (versions plus an
append-only event list). Approval is an OpenSSH signature (SSHSIG, namespace
nexus-script-approval) made with ssh-keygen. The probe uses a software
ed25519 key; the hardware key and its touch are not exercised here.

usage: uv run --no-project --with 'lupa==2.8' --with cryptography python -I registry_probe.py
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile

import lupa.lua54 as L

HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("static_check", HERE / "static_check.py")
sc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sc)

NAMESPACE = "nexus-script-approval"
LIMITS = {"instructions": 50_000_000, "seconds": 5.0, "memory": 64 * 1024 * 1024}

BOOT = r"""
return function(dispatch, grants, max_ins, max_secs, co_mode, rearm, guard)
  local load, sethook, clock, pairs, ipairs, error, pcall, type, find, tpack, tunpack =
        load, debug.sethook, os.clock, pairs, ipairs, error, pcall, type, string.find,
        table.pack, table.unpack
  local cocreate, coresume, cowrap, coyield = coroutine.create, coroutine.resume, coroutine.wrap, coroutine.yield
  local function copy(t, drop) local c = {} for k, v in pairs(t) do if k ~= drop then c[k] = v end end return c end
  local sstring = copy(string, "dump")
  getmetatable("").__index = sstring
  local env = {assert=assert, error=error, ipairs=ipairs, next=next, pairs=pairs, pcall=pcall,
    select=select, tonumber=tonumber, tostring=tostring, type=type, rawequal=rawequal,
    rawlen=rawlen, string=sstring, table=copy(table), math=copy(math), utf8=copy(utf8)}
  local mk = load("local d, name = ... return function(...) local r = d(name, ...) "
    .. "if r.err then error(r.err, 2) end return r.value end", "=verb", "t", {error = error})
  for _, verb in ipairs(grants) do
    local ns, m = verb:match("^(%w+)%.([%w_]+)$")
    env[ns] = env[ns] or {}
    env[ns][m] = mk(dispatch, verb)
  end
  local count, start, tripped = 0, clock(), nil
  -- guard: the script's pcall never swallows a tripped limit or a stack
  -- overflow (at the C-stack limit the Lua hook itself cannot be called)
  if guard then
    env.pcall = function(f, ...)
      local r = tpack(pcall(f, ...))
      if not r[1] then
        if tripped then error(tripped, 0) end
        if type(r[2]) == "string" and find(r[2], "stack overflow", 1, true) then
          tripped = "limit:stack" error(tripped, 0)
        end
      end
      return tunpack(r, 1, r.n)
    end
  end
  local hook
  hook = function()
    count = count + 10000
    if not tripped and count > max_ins then tripped = "limit:instructions" end
    if not tripped and clock() - start > max_secs then tripped = "limit:wall" end
    if tripped then
      -- rearm: once tripped, fire on every instruction, so a pcall that
      -- catches the error is unwound by the next instruction outside it
      if rearm then sethook(hook, "", 1) end
      error(tripped, 0)
    end
  end
  if co_mode then
    local function wcreate(f) local co = cocreate(f) sethook(co, hook, "", 10000) return co end
    local w = {create = wcreate, resume = coresume, yield = coyield}
    if co_mode == "native_wrap" then w.wrap = cowrap end
    if co_mode == "wrapped_wrap" then
      w.wrap = function(f)
        local co = wcreate(f)
        return function(...)
          local r = tpack(coresume(co, ...))
          if not r[1] then error(r[2], 0) end
          return tunpack(r, 2, r.n)
        end
      end
    end
    env.coroutine = w
  end
  local G = _G for k in pairs(G) do G[k] = nil end
  return function(src, args)
    if src:byte(1) == 27 then return false, "binary chunk refused" end
    local f, e = load(src, "=script", "t", env)
    if not f then return false, e end
    count, start = 0, clock()
    sethook(hook, "", 10000)
    local ok, r = pcall(f, args)
    sethook()
    return ok, r
  end
end
"""


def _refuse(obj, name, is_setting):
    raise AttributeError("no attribute access")


def to_json(v, depth=0):
    t = L.lua_type(v)
    if t is None:
        return v
    if depth > 32:
        raise ValueError("result: nesting over 32")
    if t != "table":
        raise ValueError(f"result: a {t} cannot leave the sandbox")
    keys = list(v.keys())
    if all(isinstance(k, int) for k in keys) and sorted(keys) == list(range(1, len(keys) + 1)):
        return [to_json(v[k], depth + 1) for k in sorted(keys)]
    if all(isinstance(k, str) for k in keys):
        return {k: to_json(v[k], depth + 1) for k in keys}
    raise ValueError("result: table with mixed keys")


def run_sandboxed(src: str, grants: set[str], backend, args=None, co_mode=None, rearm=True,
                  limits=None, guard=True):
    """Layer 2: fresh runtime, only granted verbs, limits. Never raises a Lua error out."""
    rt = L.LuaRuntime(register_eval=False, register_builtins=False,
                      unpack_returned_tuples=False, attribute_filter=_refuse,
                      max_memory=(limits or LIMITS)["memory"])

    def dispatch(verb, *a):
        try:
            if verb not in grants:
                return rt.table_from({"err": f"verb not granted: {verb}"})
            if any(not isinstance(x, (str, int, float, bool, type(None))) for x in a):
                return rt.table_from({"err": f"{verb}: arguments must be plain values"})
            return rt.table_from({"value": backend(verb, list(a))}, recursive=True)
        except Exception as e:  # noqa: BLE001 - the dispatcher never raises into Lua
            return rt.table_from({"err": f"{verb}: {type(e).__name__}: {e}"})

    lim = limits or LIMITS
    run = rt.execute(BOOT)(dispatch, rt.table_from(sorted(grants)), lim["instructions"],
                           lim["seconds"], co_mode, rearm, guard)
    try:
        ok, r = run(src, rt.table_from(args or {}, recursive=True))
    except L.LuaMemoryError:
        return {"error": "limit:memory"}
    except L.LuaError as e:
        # a rearmed hook fires on the host wrapper's next instruction too, so a
        # tripped limit arrives here as a LuaError rather than through pcall
        return {"error": str(e)}
    if not ok:
        return {"error": str(r)}
    try:
        return {"value": to_json(r)}
    except ValueError as e:
        return {"error": str(e)}


SCALARS = {"string": str, "number": (int, float), "boolean": bool}


def shape_ok(value, shape) -> bool:
    if isinstance(shape, str):
        return isinstance(value, SCALARS[shape]) and not (shape == "number" and isinstance(value, bool))
    if shape["type"] == "array":
        return isinstance(value, list) and all(shape_ok(x, shape["items"]) for x in value)
    if shape["type"] == "object":
        return isinstance(value, dict) and set(value) == set(shape["keys"]) and \
            all(shape_ok(value[k], s) for k, s in shape["keys"].items())
    return False


# ---- SSHSIG (OpenSSH PROTOCOL.sshsig), ed25519, verified in-process ----
def _ssh_string(b: bytes, i: int) -> tuple[bytes, int]:
    n = int.from_bytes(b[i:i + 4], "big")
    return b[i + 4:i + 4 + n], i + 4 + n


def sshsig_verify(message: bytes, armored: str, anchor_pub: str) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    body = "".join(ln for ln in armored.strip().splitlines() if not ln.startswith("-----"))
    blob = base64.b64decode(body)
    if blob[:6] != b"SSHSIG" or int.from_bytes(blob[6:10], "big") != 1:
        return False
    pub, i = _ssh_string(blob, 10)
    ns, i = _ssh_string(blob, i)
    reserved, i = _ssh_string(blob, i)
    halg, i = _ssh_string(blob, i)
    sig, i = _ssh_string(blob, i)
    if ns != NAMESPACE.encode() or halg != b"sha512":
        return False
    if pub != base64.b64decode(anchor_pub.split()[1]):
        return False  # signed by a key that is not the anchor
    ktype, j = _ssh_string(pub, 0)
    raw_pub, _ = _ssh_string(pub, j)
    stype, j = _ssh_string(sig, 0)
    raw_sig, _ = _ssh_string(sig, j)
    if ktype != b"ssh-ed25519" or stype != b"ssh-ed25519":
        return False
    pre = (b"SSHSIG" + len(ns).to_bytes(4, "big") + ns + len(reserved).to_bytes(4, "big")
           + reserved + len(halg).to_bytes(4, "big") + halg + (64).to_bytes(4, "big")
           + hashlib.sha512(message).digest())
    try:
        Ed25519PublicKey.from_public_bytes(raw_pub).verify(raw_sig, pre)
        return True
    except InvalidSignature:
        return False


class Key:
    def __init__(self, d: pathlib.Path, label: str):
        self.path = d / label
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", label, "-f",
                        str(self.path)], check=True)
        self.pub = (d / f"{label}.pub").read_text().strip()

    def sign(self, payload: bytes) -> str:
        f = self.path.with_suffix(".payload")
        f.write_bytes(payload)
        f.with_suffix(".payload.sig").unlink(missing_ok=True)  # ssh-keygen prompts to overwrite
        subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", str(self.path), "-n", NAMESPACE,
                        str(f)], check=True, capture_output=True)
        return f.with_suffix(".payload.sig").read_text()


def openssh_verify(d: pathlib.Path, anchor: str, payload: bytes, sig: str) -> bool:
    (d / "allowed").write_text(f"sam {anchor}\n")
    (d / "check.sig").write_text(sig)
    r = subprocess.run(["ssh-keygen", "-Y", "verify", "-f", str(d / "allowed"), "-I", "sam",
                        "-n", NAMESPACE, "-s", str(d / "check.sig")], input=payload,
                       capture_output=True)
    return r.returncode == 0


# ---- registry stand-in ----
class Registry:
    def __init__(self, anchor: str):
        self.anchor, self.versions, self.events = anchor, {}, []

    @staticmethod
    def payload(kind, name, sha, grants, shape) -> bytes:
        return json.dumps({"kind": kind, "name": name, "sha256": sha, "grants": sorted(grants),
                           "shape": shape}, sort_keys=True, separators=(",", ":")).encode()

    def evaluate(self, src, grants, fixtures, shape) -> dict:
        try:
            sc.check(src.encode(), set(grants))
            static = "passed"
        except sc.Refused as e:
            return {"static": str(e), "trial": "not run"}
        trial = []
        for fx in fixtures:
            def backend(verb, a, fx=fx):
                key = f"{verb} {json.dumps(a)}"
                if key not in fx["responses"]:
                    raise KeyError(f"no fixture for {key}")
                return fx["responses"][key]
            r = run_sandboxed(src, set(grants), backend, fx.get("args"))
            if "error" in r:
                trial.append(f"error: {r['error']}")
            elif not shape_ok(r["value"], shape):
                trial.append(f"shape mismatch: {json.dumps(r['value'])[:60]}")
            else:
                trial.append("ok")
        return {"static": static, "trial": trial}

    def register(self, name, src, grants, fixtures, shape, registrant) -> str:
        sha = hashlib.sha256(src.encode()).hexdigest()
        self.versions[(name, sha)] = {"source": src, "grants": sorted(grants), "fixtures": fixtures,
                                      "shape": shape, "registrant": registrant,
                                      "report": self.evaluate(src, grants, fixtures, shape)}
        return sha

    def approve(self, name, sha, key: Key, kind="approve") -> str:
        v = self.versions[(name, sha)]
        rep = self.evaluate(v["source"], v["grants"], v["fixtures"], v["shape"])  # recomputed
        if kind == "approve" and (rep["static"] != "passed" or any(t != "ok" for t in rep["trial"])):
            return f"approve refused: {rep}"
        p = self.payload(kind, name, sha, v["grants"], v["shape"])
        self.events.append({"name": name, "sha256": sha, "kind": kind, "payload": p,
                            "sig": key.sign(p)})
        return f"{kind} recorded"

    def state(self, name, sha) -> str:
        v = self.versions.get((name, sha))
        if v is None:
            return "unregistered"
        evs = [e for e in self.events if e["name"] == name and e["sha256"] == sha]
        if any(e["kind"] == "revoke" for e in evs):
            return "revoked"  # terminal; any revoke row, fail closed
        want = self.payload("approve", name, sha, v["grants"], v["shape"])
        if any(e["kind"] == "approve" and e["payload"] == want
               and sshsig_verify(want, e["sig"], self.anchor) for e in evs):
            return "approved"
        return "pending"

    def run_script(self, name, sha, args, backend) -> dict:
        v = self.versions.get((name, sha))
        if v is None:
            return {"refused": "not registered"}
        if hashlib.sha256(v["source"].encode()).hexdigest() != sha:
            return {"refused": "source does not match the pinned sha256"}
        st = self.state(name, sha)
        if st != "approved":
            return {"refused": st}
        try:
            sc.check(v["source"].encode(), set(v["grants"]))
        except sc.Refused as e:
            return {"refused": f"static check: {e}"}
        r = run_sandboxed(v["source"], set(v["grants"]), backend, args)
        if "value" in r and not shape_ok(r["value"], v["shape"]):
            return {"error": "output shape"}
        return r


def live_backend(verb, a):
    if verb == "memory.search":
        return [{"title": "alpha"}, {"title": "beta"}]
    if verb == "memory.get":
        return {"content": f"live:{a[1]}"}
    raise KeyError(verb)


SRC = sc.pathlib.Path(HERE / "corpus" / "ok_verbs.lua").read_text()
GRANTS = ["memory.get", "memory.search"]
SHAPE = {"type": "array", "items": "string"}
FIXTURES = [{"args": {}, "responses": {
    'memory.search ["lua", "nexus", 5]': [{"title": "t1"}],
    'memory.get ["nexus", "t1"]': {"content": "fixture body"}}}]


WRAP_LOOP = "local f = coroutine.wrap(function() while true do end end) return f()"
CATCH_LOOP = "while true do pcall(function() while true do end end) end"
NESTED = "local function g() while true do pcall(g) end end g()"
CASES = {
    # (source, coroutine mode, rearm the hook on trip, guarded pcall)
    "native_wrap": (WRAP_LOOP, "native_wrap", True, True),
    "wrapped_wrap": (WRAP_LOOP, "wrapped_wrap", True, True),
    "create_only": ("local co = coroutine.create(function() while true do end end) "
                    "return coroutine.resume(co)", "create_only", True, True),
    "pcall_catch_raw": (CATCH_LOOP, None, False, False),
    "pcall_catch_rearm": (CATCH_LOOP, None, True, False),
    "pcall_nested_rearm": (NESTED, None, True, False),
    "pcall_nested_guarded": (NESTED, None, True, True),
    "pcall_catch_guarded_norearm": (CATCH_LOOP, None, False, True),
}


def sub_case(mode: str) -> str:
    try:
        out = subprocess.run([sys.executable, "-I", __file__, "--case", mode], capture_output=True,
                             text=True, timeout=8)
        return out.stdout.strip() or out.stderr.strip()[-120:]
    except subprocess.TimeoutExpired:
        return "TIMEOUT after 8 s: the loop escaped the hook"


def main() -> None:
    def show(label, r):
        print(f"{label:58} {r}")

    with tempfile.TemporaryDirectory() as td:
        d = pathlib.Path(td)
        sam, agent = Key(d, "sam"), Key(d, "agent")
        reg = Registry(sam.pub)
        sha = reg.register("ok_verbs", SRC, GRANTS, FIXTURES, SHAPE, "agent:developer")
        show("A register: report recorded on the entry", reg.versions[("ok_verbs", sha)]["report"])
        show("A state after register", reg.state("ok_verbs", sha))
        show("B run before approval", reg.run_script("ok_verbs", sha, {}, live_backend))
        show("G approval signed by a key that is not the anchor", reg.approve("ok_verbs", sha, agent))
        show("G state after the forged approval", reg.state("ok_verbs", sha))
        show("C approve with the anchor key", reg.approve("ok_verbs", sha, sam))
        ev = reg.events[-1]
        show("C in-process SSHSIG verify / ssh-keygen -Y verify",
             (sshsig_verify(ev["payload"], ev["sig"], sam.pub),
              openssh_verify(d, sam.pub, ev["payload"], ev["sig"])))
        show("C run approved, live backend", reg.run_script("ok_verbs", sha, {}, live_backend))
        edited = SRC.replace("return out", "out[#out + 1] = 'extra'\nreturn out")
        sha2 = reg.register("ok_verbs", edited, GRANTS, FIXTURES, SHAPE, "agent:developer")
        show("D edit -> new version, state", (sha2[:12], reg.state("ok_verbs", sha2)))
        show("D run the edited version", reg.run_script("ok_verbs", sha2, {}, live_backend))
        show("D run the approved version still", reg.run_script("ok_verbs", sha, {}, live_backend))
        saved = dict(reg.versions[("ok_verbs", sha)])
        reg.versions[("ok_verbs", sha)]["source"] = edited
        show("E source swapped under the approved sha", reg.run_script("ok_verbs", sha, {}, live_backend))
        reg.versions[("ok_verbs", sha)] = dict(saved, grants=sorted(GRANTS + ["memory.list"]))
        show("F grants widened after approval", reg.run_script("ok_verbs", sha, {}, live_backend))
        reg.versions[("ok_verbs", sha)] = saved
        show("H revoke with the anchor key", reg.approve("ok_verbs", sha, sam, kind="revoke"))
        show("H run revoked", reg.run_script("ok_verbs", sha, {}, live_backend))
        old = next(e for e in reg.events if e["kind"] == "approve" and e["sha256"] == sha
                   and sshsig_verify(e["payload"], e["sig"], sam.pub))
        reg.events.append(dict(old))
        show("H replay of the old approval after revoke", reg.state("ok_verbs", sha))
        bad_fx = [{"args": {}, "responses": {'memory.search ["lua", "nexus", 5]': [{"title": "t9"}]}}]
        sha3 = reg.register("missing_fixture", SRC, GRANTS, bad_fx, SHAPE, "agent:developer")
        show("I trial: a verb call with no fixture", reg.versions[("missing_fixture", sha3)]["report"])
        show("I approve refuses it", reg.approve("missing_fixture", sha3, sam)[:60])
        sha4 = reg.register("wrong_shape", SRC, GRANTS, FIXTURES, {"type": "array", "items": "number"},
                            "agent:developer")
        show("I trial: output shape mismatch", reg.versions[("wrong_shape", sha4)]["report"])
        sha5 = reg.register("escape", "return load('return 1')()", [], [{"args": {}, "responses": {}}],
                            "number", "agent:developer")
        show("I static check failure is recorded, trial not run", reg.versions[("escape", sha5)]["report"])

    # layer 2 with the static check bypassed (as if the checker had a bug)
    show("J ungranted verb, checker bypassed",
         run_sandboxed('return memory.list("p")', {"memory.get"}, live_backend))
    show("J verb namespace absent, checker bypassed",
         run_sandboxed('return git.diff_names("HEAD")', {"memory.get"}, live_backend))
    show("J backend exception reaches the script as a string",
         run_sandboxed('return memory.get("p","t")', {"memory.get"},
                       lambda v, a: (_ for _ in ()).throw(PermissionError("backend refused"))))
    show("K dynamic dump key at run time (passes static)",
         run_sandboxed((HERE / "corpus" / "ok_layer2_dynamic_dump.lua").read_text(), set(), live_backend))
    show("L while true", run_sandboxed("while true do end", set(), live_backend))
    show("L string.rep 1e9", run_sandboxed('return #string.rep("x", 1e9)', set(), live_backend))
    show("L load reachable at run time?", run_sandboxed("return load == nil and debug == nil", set(), live_backend))
    show("M coroutine.wrap native, create wrapped", sub_case("native_wrap"))
    show("M coroutine.wrap wrapped too", sub_case("wrapped_wrap"))
    show("M coroutine.create wrapped", sub_case("create_only"))
    show("N pcall swallows the limit: raw pcall, no rearm", sub_case("pcall_catch_raw"))
    show("N pcall swallows the limit: raw pcall, rearmed hook", sub_case("pcall_catch_rearm"))
    show("N pcall recursion to the C-stack limit: rearmed hook", sub_case("pcall_nested_rearm"))
    show("N pcall recursion to the C-stack limit: guarded pcall", sub_case("pcall_nested_guarded"))
    show("N pcall swallows the limit: guarded pcall, no rearm", sub_case("pcall_catch_guarded_norearm"))
    show("N guarded pcall still catches an ordinary error",
         run_sandboxed('local ok, e = pcall(error, "boom", 0) return {tostring(ok), e}', set(), live_backend))


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--case":
        src, co_mode, rearm, guard = CASES[sys.argv[2]]
        # wall time is CPU time here (os.clock); 3 s, so a hang is the 8 s timeout
        lim = dict(LIMITS, seconds=3.0)
        print(run_sandboxed(src, set(), live_backend, co_mode=co_mode, rearm=rearm, limits=lim,
                            guard=guard))
    else:
        main()
