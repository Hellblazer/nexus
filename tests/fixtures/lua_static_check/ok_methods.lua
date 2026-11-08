-- expect: ok
local s = "abc"
local ok, err = pcall(error, "boom")
return {s:upper(), ("x"):rep(3), string.format("%s %d", s, 3), tostring(ok), err}
