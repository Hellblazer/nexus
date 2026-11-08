-- expect: global
local co = coroutine.create(function() while true do end end)
return coroutine.resume(co)
