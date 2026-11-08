-- expect: global
setmetatable(string, {__index = function() end})
return 1
