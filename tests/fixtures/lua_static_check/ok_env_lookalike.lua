-- expect: ok
local my_ENV, _ENVx, ENV = 1, 2, 3
local _ENV1, a1_ENV, ENV_ENV, _ENV_ = 4, 5, 6, 7
return my_ENV + _ENVx + ENV + _ENV1 + a1_ENV + ENV_ENV + _ENV_
