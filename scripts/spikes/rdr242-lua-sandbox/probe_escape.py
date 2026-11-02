# Escape probes for a lupa runtime with an empty script environment.
# usage: venv/bin/python -I probe_escape.py <lupa module, e.g. lua54 or luajit21>
#
# Host boot runs in full _G, captures the loader locals, optionally wipes _G,
# and returns an api table with only: compile(src, name, env), bc_probe().
# Each probe script runs in a fresh env table holding only host objects.
import importlib
import os
import sys

MOD = sys.argv[1]
M = importlib.import_module("lupa." + MOD)
LR = M.LuaRuntime

BOOT = r"""
local G = _G
local is51 = (_VERSION == "Lua 5.1") or (jit ~= nil)
local compile_src
if is51 then
  local ls, sf = loadstring, setfenv
  compile_src = function(src, name, env)
    local f, err = ls(src, name)
    if not f then return nil, err end
    sf(f, env)
    return f
  end
else
  local ld = load
  compile_src = function(src, name, env)
    return ld(src, name, "t", env)
  end
end
local sdump = string.dump
local api = { compile = compile_src }
api.bc_probe = function()
  local bc = sdump(function() return 7 end)
  return compile_src(bc, "bc", {})
end
api.leak = __LEAK__ and function() return G end or nil
api.sethook = debug and debug.sethook or nil
api.is51 = is51
if __WIPE__ then
  for k in pairs(G) do G[k] = nil end
end
return api
"""


def filt(obj, name, is_setting):
    if isinstance(name, str) and not name.startswith("_"):
        return name
    raise AttributeError("access denied: %s" % (name,))


class Host:
    secret = "S-VALUE"
    mod = os  # a module reachable through a non-underscore attribute
    @staticmethod
    def read():
        return "read-ok"


def make(wipe=True, leak=False, filt_on=True):
    rt = LR(
        register_eval=False,
        register_builtins=False,
        unpack_returned_tuples=True,
        attribute_filter=filt if filt_on else None,
    )
    src = BOOT.replace("__WIPE__", "true" if wipe else "false").replace(
        "__LEAK__", "true" if leak else "false"
    )
    api = rt.execute(src)
    return rt, api


def _kind(v):
    return "nil" if v is None else type(v).__name__


def envt(rt, api=None):
    d = {"host": Host(), "kind": _kind}
    if api is not None and api.leak:
        d["leak"] = api.leak
    return rt.table_from(d)


def run_probe(src, wipe=True, leak=False, filt_on=True):
    rt, api = make(wipe, leak, filt_on)
    src = src.replace("type(", "kind(")
    r = api.compile(src, "probe", envt(rt, api))
    if isinstance(r, tuple):
        return "COMPILE-FAIL", str(r[1])[:160]
    try:
        out = r()
        return "RETURNED", "%s:%s" % (type(out).__name__, str(out)[:160])
    except Exception as e:  # lupa.LuaError or AttributeError from the filter
        return "ERROR", "%s: %s" % (type(e).__name__, str(e)[:160])


PROBES = [
    ("global_io", "return type(io)"),
    ("global_os", "return type(os)"),
    ("os_execute", 'return os.execute("true")'),
    ("require_fn", "return type(require)"),
    ("require_os", 'return require("os")'),
    ("load_fn", "return type(load)"),
    ("loadstring_fn", "return type(loadstring)"),
    ("dofile_loadfile", "return kind(dofile), kind(loadfile)"),
    ("debug_lib", "return type(debug)"),
    ("python_module", "return type(python)"),
    ("string_global", "return type(string)"),
    ("getmetatable_fn", "return type(getmetatable)"),
    ("setmetatable_fn", "return type(setmetatable)"),
    ("getfenv_setfenv", "return kind(getfenv), kind(setfenv)"),
    ("rawget_fn", "return type(rawget)"),
    ("collectgarbage_fn", "return type(collectgarbage)"),
    ("pairs_fn", "return type(pairs)"),
    ("_G_global", "return type(_G)"),
    ("package_lib", "return type(package)"),
    ("ffi_luajit", "return type(ffi)"),
    ("jit_global", "return type(jit)"),
    # Reachable without any global: the string metatable __index is the string library.
    ("string_meta_index", 'return type(("").__index)'),
    ("string_dump_via_meta", 'return type(("").dump)'),
    ("string_dump_produces_bytecode", 'return #(("").dump(function() return 1 end))'),
    # Host object, attribute filter on.
    ("host_secret", "return host.secret"),
    ("host_call", "return host.read()"),
    ("host_dunder_class", "return host.__class__"),
    ("host_dunder_globals", "return host.read.__globals__"),
    ("host_module_attr_type", "return type(host.mod)"),
    ("host_module_attr_call", "return host.mod.getcwd()"),
    ("host_module_attr_system", "return type(host.mod.system)"),
]

LEAK_PROBES = [
    ("leaked_closure_reaches_io", "return type(leak().io)"),
    ("leaked_closure_reaches_os", "return type(leak().os)"),
]


def run_seq_contamination():
    """Two scripts on one runtime: does one leave state the next can see?"""
    rt, api = make(True, False, True)
    e = envt(rt)
    f1 = api.compile('("").__index.planted = 1 return "set"', "a", e)
    r1 = f1()
    f2 = api.compile('return tostring(("").planted)', "b", e)
    r2 = f2()
    return r1, r2


def run_bytecode_host_compile(filt_on=True):
    rt, api = make(True, False, filt_on)
    r = api.bc_probe()
    if isinstance(r, tuple):
        return "REJECTED", str(r[1])[:160]
    return "ACCEPTED-AND-RUN", str(r())


def main():
    print("module=%s lua_version=%s" % (MOD, LR().lua_version if False else "see-runtime"))
    rt0 = LR()
    print("implementation=%s" % rt0.lua_implementation)
    print("== empty-env escape probes, _G wiped, attribute filter on")
    for name, src in PROBES:
        status, detail = run_probe(src)
        print("%-30s %-14s %s" % (name, status, detail))
    print("== attribute filter OFF, host object")
    for name, src in [
        ("host_dunder_class_nofilter", "return host.__class__"),
        ("host_module_attr_call_nofilter", "return host.mod.getcwd()"),
    ]:
        status, detail = run_probe(src, filt_on=False)
        print("%-30s %-14s %s" % (name, status, detail))
    print("== host passed a closure from the full runtime (host hygiene)")
    for name, src in LEAK_PROBES:
        status, detail = run_probe(src, wipe=True, leak=True)
        print("%-30s %-14s %s" % (name, status, detail))
    print("== _G wiped vs not wiped, script env probe")
    for wipe in (True, False):
        status, detail = run_probe("return type(io)", wipe=wipe)
        print("wipe=%s io_in_script=%s %s" % (wipe, status, detail))
    print("== host-side compile of bytecode in 't' context")
    print(run_bytecode_host_compile())


if __name__ == "__main__":
    main()
