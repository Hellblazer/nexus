-- expect: ns-value
local n = 0
for k in pairs(string) do n = n + 1 end
return n
