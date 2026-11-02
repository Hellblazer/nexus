import sys
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
sys.argv = [sys.argv[0], sys.argv[1]]
import probe_escape as P
for name, src in P.LEAK_PROBES:
    print("wipe=False", name, *P.run_probe(src, wipe=False, leak=True))
print("bytecode_host_compile", P.run_bytecode_host_compile())
