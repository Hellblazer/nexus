-- expect: ok
-- passes the static check by design: the key is built at run time on a value
-- the checker cannot type. The sandbox (layer 2) must return nil for it.
local s, k = "", "du" .. "mp"
return s[k] == nil
