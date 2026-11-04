-- expect: global
local os = require "os"
return os.getenv("HOME")
