-- expect: field-dump
local s = ""
local function f() return s.dump end
return f()
