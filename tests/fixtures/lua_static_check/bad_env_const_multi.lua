-- expect: env
local a, _ENV <const> = 1, 5
x = load
return a
