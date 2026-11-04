"""RDR-242 design probe: one JSON dispatcher is the only Python object in Lua."""
import json, sys
import lupa.lua54 as L

def refuse(obj, name, is_setting):
    raise AttributeError("no attribute access")

def dispatch(verb, args_json):
    args = json.loads(args_json)
    if verb != "echo.args":
        raise ValueError("verb not granted: " + verb)
    return json.dumps(args)

rt = L.LuaRuntime(register_eval=False, register_builtins=False,
                  unpack_returned_tuples=False, attribute_filter=refuse)
build = rt.execute(r'''
return function(dispatch)
local load, setmetatable, getmetatable, sethook = load, setmetatable, getmetatable, debug.sethook
local function copy(t, drop) local c = {} for k, v in pairs(t) do if k ~= drop then c[k] = v end end return c end
local sstring = copy(string, "dump")
getmetatable("").__index = sstring
local env = {assert=assert, error=error, ipairs=ipairs, next=next, pairs=pairs, pcall=pcall,
  select=select, tonumber=tonumber, tostring=tostring, type=type, rawequal=rawequal, rawlen=rawlen,
  string=sstring, table=copy(table), math=copy(math), utf8=copy(utf8)}
-- verb functions built in a chunk whose env holds only the dispatcher
local mk = load("local d = ... return function(a) return d('echo.args', a) end", "verbs", "t", {})
env.echo = {args = mk(dispatch)}
env.raw = function(v, a) return dispatch(v, a) end
local G = _G for k in pairs(G) do G[k] = nil end
return function(src)
  if src:byte(1) == 27 then return nil, "binary chunk refused" end
  return load(src, "script", "t", env)
end
end
''')
compile_ = build(dispatch)
cases = {
  "verb_call": 'return echo.args(\'{"x":1}\')',
  "dump_via_meta": 'return ("").dump',
  "dispatch_attr": 'return pcall(function() return echo.args.__self end)',
  "ungranted_verb": 'return pcall(raw, "memory.put", "{}")',
  "globals": 'local n = {} for _, k in ipairs({"io","os","require","load","debug","package","python","getmetatable","setmetatable","coroutine","_G","rawset","collectgarbage"}) do if _ENV[k] ~= nil then n[#n+1] = k end end return table.concat(n, ",")',
  "binary_chunk": "\x1bLua",
  "exc_attr": 'local ok, e = pcall(raw, "memory.put", "{}") return type(e), pcall(function() return e.args end), pcall(function() return e.__traceback__ end), pcall(tostring, e)',
  "exc_call": 'local ok, e = pcall(raw, "memory.put", "{}") return pcall(e, 1)',
}
for name, src in cases.items():
    f = compile_(src)
    if isinstance(f, tuple) or f is None:
        print(f"{name:16} COMPILE {f}"); continue
    try:
        r = f()
        print(f"{name:16} -> {r!r}")
    except Exception as e:
        print(f"{name:16} ERROR {type(e).__name__}: {str(e)[:90]}")
