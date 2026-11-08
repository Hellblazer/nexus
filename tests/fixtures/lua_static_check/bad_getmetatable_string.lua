-- expect: global
getmetatable("").__index = {}
return 1
