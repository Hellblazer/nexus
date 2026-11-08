-- expect: field-dump
local t = {}
local function f(g) t.dump = g end
f(1)
return 1
