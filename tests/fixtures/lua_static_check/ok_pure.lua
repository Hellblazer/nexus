-- expect: ok
local function fib(n) if n < 2 then return n end return fib(n - 1) + fib(n - 2) end
local t = {}
for i = 1, 10 do t[#t + 1] = fib(i) end
local s = table.concat(t, ",")
local n <const> = 7 // 2
::top::
return {s = s, n = n, up = string.upper("ok"), m = math.max(1, 2), len = utf8.len("abc")}
