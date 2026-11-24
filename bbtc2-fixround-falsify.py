#!/usr/bin/env python3
"""apply|restore <name> <outdir>: fault injection for the nexus-bbtc2 fix round. Prints applied=<n>."""
import sys, shutil, os, re
name, action, out = sys.argv[2], sys.argv[1], sys.argv[3]
J = "src/main/java/dev/nexus/service/vectors/"
X = "src/main/resources/db/changelog/vectors-060-text-gate-probe-by-collection.xml"
def sub(path, old, new, count=None):
    s = open(path).read(); n = s.count(old)
    if count is not None: assert n == count, (name, old, n)
    open(path, "w").write(s.replace(old, new)); return n
def f1():
    p = J + "LexicalGateProbe.java"; s = open(p).read()
    a = "                    PgSession.setSearchStatementTimeout(ctx, bound.getAsInt());\n"
    b = "                    Result<? extends Record> per = ctx.selectFrom(fn).fetch();\n"
    assert s.count(a) == 1 and s.count(b) == 1
    s = s.replace(a, "").replace(b, b + a); open(p, "w").write(s); return 1
def f9():
    s = open(X).read(); n = 0
    parts = re.split(r"(?=text_gate_probe_by_collection_(?:384|768|1024)\()", s); out_ = []
    for part in parts:
        if part.startswith("text_gate_probe_by_collection_768("):
            n += part.count("OVER (PARTITION BY u.coll)")
            part = part.replace("OVER (PARTITION BY u.coll)", "OVER (PARTITION BY u.coll ORDER BY u.ch)")
        out_.append(part)
    open(X, "w").write("".join(out_)); return n
FALS = {
 "F1": (J + "LexicalGateProbe.java", f1),
 "F2": (X, lambda: sub(X, "         WHERE EXISTS (SELECT 1 FROM nexus.chunk_live_owners(p_tenant, u.coll, u.ch))", "         WHERE true")),
 "F3": (J + "LexicalGateProbe.java", lambda: sub(J + "LexicalGateProbe.java", "return selectiveGateMax + 1;", "return selectiveGateMax;", 1)),
 "F4": (X, lambda: sub(X, "IF pg_catalog.cardinality(v_coll) > v_ceiling THEN", "IF false THEN")),
 "F5": (X, lambda: sub(X, "SET jit = off\n", "")),
 "F6": (X, lambda: sub(X, "SET jit = off\n", "SET jit = off\nSET max_parallel_workers_per_gather = 2\n")),
 "F7": (J + "LexicalGateProbe.java", lambda: sub(J + "LexicalGateProbe.java", "remainingMs / 2", "remainingMs", 1)),
 "F8": (J + "SharedLexicalProbe.java", lambda: sub(J + "SharedLexicalProbe.java", '? "not_started"', '? "pending"', 1)),
 "F9": (X, f9),
}
path, fn = FALS[name]
bak = os.path.join(out, "orig-" + name + "-" + os.path.basename(path))
if action == "apply":
    shutil.copy(path, bak); print(f"{name} applied={fn()}")
else:
    shutil.copy(bak, path); print(f"{name} restored same={open(bak).read() == open(path).read()}")
