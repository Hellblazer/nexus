---
type: regex
pattern: 'os\.system|shell injection|command injection|injection|shell=True|subprocess'
flags: i
match: contains
---

Planted defect 2: `index()` runs `os.system("chmod 666 " + cache_path)`,
and `--cache-path` comes from the command line.
