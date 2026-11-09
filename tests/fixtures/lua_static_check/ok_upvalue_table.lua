-- expect: ok
local M = {x = 1}
local function get() return M.x end
local function put(v) M.y = v end
put(get())
return M.y
