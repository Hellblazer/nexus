# usage: python -I probe_limits.py <module> <probe> [jitoff]
import sys, time
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
MOD, PROBE = sys.argv[1], sys.argv[2]
JITOFF = "jitoff" in sys.argv
sys.argv = [sys.argv[0], MOD]
import probe_escape as P
import importlib

LIMIT_SRC = {
    "inst_while": "while true do end",
    "inst_forloop": "local x = 0 for i = 1, 1e12 do x = x + i end return x",
    "inst_hot_call": "local function f(a) return a + 1 end local x = 0 while true do x = f(x) end",
    "mem_string": "return #rep('a', 1000000000)",
    "mem_table": "local t = {} for i = 1, 1e9 do t[i] = i end return #t",
    "stack_recursion": "local function r() return 1 + r() end return r()",
    "result_size": "return rep('x', 200000000)",
    "coroutine_hook": "local co = co_create(function() while true do end end) return co_resume(co)",
}

def build(max_memory=None):
    LR = importlib.import_module("lupa." + MOD).LuaRuntime
    kw = dict(register_eval=False, register_builtins=False, unpack_returned_tuples=True,
              attribute_filter=P.filt)
    if max_memory:
        kw["max_memory"] = max_memory
    rt = LR(**kw)
    global HOOK
    HOOK = rt.execute("local e = error return function() e('INSTRUCTION-HOOK-FIRED') end")
    co = rt.execute("return coroutine")  # captured before the wipe
    global JIT
    JIT = rt.execute("return jit")  # None on PUC Lua
    global REP
    REP = rt.execute("return string.rep")
    api = rt.execute(P.BOOT.replace("__WIPE__", "true").replace("__LEAK__", "false"))
    return rt, api, co

def main():
    src = LIMIT_SRC[PROBE]
    mm = 64 * 1024 * 1024 if PROBE.startswith("mem_") else (1024 * 1024 * 1024 if PROBE == "result_size" else None)
    rt, api, co = build(mm)
    env = {"host": P.Host(), "kind": P._kind, "rep": REP}
    if PROBE == "coroutine_hook":
        env["co_create"] = co.create
        env["co_resume"] = co.resume
    f = api.compile(src, "limit", rt.table_from(env))
    if isinstance(f, tuple):
        print("COMPILE-FAIL", f); return
    if JITOFF:
        if JIT is not None:
            JIT.off()
        else:
            print("NO-JIT-MODULE")
    if PROBE.startswith("inst_") or PROBE == "coroutine_hook":
        hook = HOOK
        api.sethook(hook, "", 100000)
    t0 = time.monotonic()
    try:
        out = f()
        dt = time.monotonic() - t0
        n = len(out) if isinstance(out, (str, bytes)) else None
        print("RETURNED %.2fs type=%s len=%s" % (dt, type(out).__name__, n))
    except Exception as e:
        dt = time.monotonic() - t0
        print("ERROR %.2fs %s %s" % (dt, type(e).__name__, str(e)[:140]))

main()
