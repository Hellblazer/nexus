"""RDR-242: how deep pcall recursion goes, and how often it ends in a C stack overflow.

usage: uv run --no-project --with 'lupa==2.8' python -I cstack_probe.py
"""
# ruff: noqa: T201
import sys, time, lupa.lua54 as L
rt = L.LuaRuntime(register_eval=False, register_builtins=False)
f = rt.execute(r'''return function(limit)
  local n, maxd, errs = 0, 0, {}
  local function g(d)
    if d > maxd then maxd = d end
    while true do
      n = n + 1
      if n > limit then error("done", 0) end
      local ok, e = pcall(g, d + 1)
      if not ok then errs[tostring(e):sub(1, 40)] = (errs[tostring(e):sub(1, 40)] or 0) + 1 end
      if e == "done" then error("done", 0) end
    end
  end
  local t = os.clock()
  pcall(g, 1)
  local s = {} for k, v in pairs(errs) do s[#s + 1] = k .. "=" .. v end
  return n, maxd, os.clock() - t, table.concat(s, "; ")
end''')
for lim in (1000, 10000, 100000):
    t = time.time(); print(lim, f(lim), round(time.time() - t, 3)); sys.stdout.flush()
