"""RDR-242: does the count hook run while a script sits at the C-stack limit?

Logs every 50th hook call (and every call once tripped) to stderr.
usage: timeout 4 uv run --no-project --with 'lupa==2.8' python -I cstack_hook_probe.py '<lua source>'
"""
# ruff: noqa: T201, BLE001
import sys, time, lupa.lua54 as L
rt = L.LuaRuntime(register_eval=False, register_builtins=False)
src = sys.argv[1]
f = rt.execute("return function(src)\n" + r'''

local sethook, clock = debug.sethook, os.clock
local fires, tripped, start = 0, false, clock()
local hook
hook = function()
  fires = fires + 1 if fires % 50 == 0 or tripped then io.stderr:write(fires, " ", tostring(tripped), "\n") end
  if fires > 500 then tripped = true end
  if tripped then sethook(hook, "", 1) error("limit", 0) end
end
local f = load(src, "=s", "t", {pcall = pcall, error = error})
sethook(hook, "", 10000)
local ok, e = pcall(f)
sethook()
return fires, tostring(ok), tostring(e), clock() - start end
''')
t = time.time()
try:
    print(f(src))
except Exception as e:
    print("LuaError", str(e)[:80])
print("elapsed", round(time.time() - t, 2))
