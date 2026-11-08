-- expect: global
local f = function() end
return debug.getupvalue(f, 1)
