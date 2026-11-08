-- expect: env
local function f()
  _ENV = nil
end
return f
