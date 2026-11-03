-- expect: ok
local load = function(x) return x end
local memory = {put = function() return "local, not the verb" end}
return load(memory.put())
