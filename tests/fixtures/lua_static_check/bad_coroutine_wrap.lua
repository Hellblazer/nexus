-- expect: global
return coroutine.wrap(function() while true do end end)()
