import sys, lupa.lua54 as L
rt = L.LuaRuntime(register_eval=False, register_builtins=False)
case = sys.argv[1]
if case == "native":   # coroutine library used directly inside Lua, hook set in Lua
    src = """debug.sethook(function() error('HOOK') end, '', 100000)
    local co = coroutine.create(function() while true do end end)
    return coroutine.resume(co)"""
    print(rt.execute(src))
elif case == "proxy":  # coroutine.create reached through a Python proxy, as the survey did
    co = rt.execute("return coroutine")
    rt.execute("debug.sethook(function() error('HOOK') end, '', 100000)")
    f = rt.execute("return function(c, r) local x = c(function() while true do end end) return r(x) end")
    print(f(co.create, co.resume))
elif case == "hookmask":  # does a new thread inherit the hook at all?
    print(rt.execute("""debug.sethook(function() end, '', 100000)
    local co = coroutine.create(function() return debug.gethook() end)
    local ok, h, m, c = coroutine.resume(co)
    debug.sethook()
    return tostring(h ~= nil), tostring(c)"""))
if case == "wrapped":  # host wrapper re-arms the hook on each new coroutine
    print(rt.execute("""local H = function() error('HOOK') end
    debug.sethook(H, '', 100000)
    local create = coroutine.create
    local function wcreate(f) local co = create(f) debug.sethook(co, H, '', 100000) return co end
    local co = wcreate(function() while true do end end)
    return coroutine.resume(co)"""))
