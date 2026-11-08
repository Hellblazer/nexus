-- expect: ok
local my_ENV, _ENVx, ENV = 1, 2, 3
return my_ENV + _ENVx + ENV
