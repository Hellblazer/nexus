# ruff: noqa: T201, PLC0415  (a probe prints its results; imports kept local to their case)
"""RDR-242 static-check spike: check a script from the bytecode Lua 5.4 compiled.

The checker never parses Lua itself. It compiles the source with Lua 5.4's own
``load(src, name, "t", {})`` in a throwaway runtime (compiling runs nothing),
dumps the function with ``string.dump`` (debug info kept), reads the 5.4
binary chunk in Python, and applies the rules to every prototype. So the
checker reads exactly what the runtime would run, and a grammar difference
between a checker and Lua cannot hide a call.

usage: uv run --no-project --with 'lupa==2.8' python -I static_check.py [corpus-dir]
"""
from __future__ import annotations

import hashlib
import pathlib
import sys

import lupa.lua54 as L

# Lua 5.4 opcodes, in lopcodes.h order. calibrate() pins the numbering.
OPNAMES = (
    "MOVE LOADI LOADF LOADK LOADKX LOADFALSE LFALSESKIP LOADTRUE LOADNIL "
    "GETUPVAL SETUPVAL GETTABUP GETTABLE GETI GETFIELD SETTABUP SETTABLE SETI "
    "SETFIELD NEWTABLE SELF ADDI ADDK SUBK MULK MODK POWK DIVK IDIVK BANDK BORK "
    "BXORK SHRI SHLI ADD SUB MUL MOD POW DIV IDIV BAND BOR BXOR SHL SHR MMBIN "
    "MMBINI MMBINK UNM BNOT NOT LEN CONCAT CLOSE TBC JMP EQ LT LE EQK EQI LTI "
    "LEI GTI GEI TEST TESTSET CALL TAILCALL RETURN RETURN0 RETURN1 FORLOOP "
    "FORPREP TFORPREP TFORCALL TFORLOOP SETLIST CLOSURE VARARG VARARGPREP "
    "EXTRAARG"
).split()
OP = {n: i for i, n in enumerate(OPNAMES)}

BASE = {"assert", "error", "ipairs", "next", "pairs", "pcall", "select",
        "tonumber", "tostring", "type", "rawequal", "rawlen"}
LIBS = {
    "string": {"byte", "char", "find", "format", "gmatch", "gsub", "len",
               "lower", "match", "pack", "packsize", "rep", "reverse", "sub",
               "unpack", "upper"},
    "table": {"concat", "insert", "move", "pack", "remove", "sort", "unpack"},
    "math": {"abs", "ceil", "cos", "deg", "exp", "floor", "fmod", "huge", "log",
             "max", "maxinteger", "min", "mininteger", "modf", "pi", "rad",
             "random", "randomseed", "sin", "sqrt", "tan", "tointeger", "type",
             "ult", "acos", "asin", "atan"},
    "utf8": {"char", "charpattern", "codepoint", "codes", "len", "offset"},
}
# Version 1 verb table (RDR-242 Technical Design 5). All reads.
VERBS = {"memory.get", "memory.list", "memory.search", "transcripts.sessions",
         "git.diff_names", "git.diff_stat", "tables.get"}
REFUSED_FIELDS = {"dump"}
MAX_SOURCE = 64 * 1024


class Refused(Exception):
    def __init__(self, rule: str, detail: str, line: int | None = None):
        super().__init__(f"{rule}: {detail}" + (f" (line {line})" if line else ""))
        self.rule, self.detail, self.line = rule, detail, line


# ---- Lua 5.4 binary chunk reader (ldump.c layout) ----
class Proto:
    __slots__ = ("linedefined", "code", "k", "upnames", "protos", "lines", "locvars")


class Reader:
    def __init__(self, b: bytes):
        self.b, self.i = b, 0

    def byte(self) -> int:
        v = self.b[self.i]
        self.i += 1
        return v

    def raw(self, n: int) -> bytes:
        v = self.b[self.i:self.i + n]
        self.i += n
        return v

    def size(self) -> int:  # big-endian 7-bit groups, last byte has 0x80
        x = 0
        while True:
            c = self.byte()
            x = (x << 7) | (c & 0x7F)
            if c & 0x80:
                return x

    def string(self) -> bytes | None:
        n = self.size()
        return None if n == 0 else self.raw(n - 1)

    def header(self) -> None:
        if self.raw(4) != b"\x1bLua" or self.byte() != 0x54 or self.byte() != 0:
            raise ValueError("not a Lua 5.4 chunk")
        if self.raw(6) != b"\x19\x93\r\n\x1a\n" or self.raw(3) != b"\x04\x08\x08":
            raise ValueError("unexpected chunk layout")
        self.raw(16)  # LUAC_INT, LUAC_NUM
        self.byte()   # main function's upvalue count

    def function(self) -> Proto:
        p = Proto()
        self.string()  # source
        p.linedefined = self.size()
        self.size()    # lastlinedefined
        self.raw(3)    # numparams, is_vararg, maxstacksize
        p.code = [int.from_bytes(self.raw(4), "little") for _ in range(self.size())]
        p.k = []
        for _ in range(self.size()):
            t = self.byte()
            if t in (3, 0x13):          # integer, float
                p.k.append(self.raw(8))
            elif t in (4, 0x14):        # short, long string
                p.k.append(self.string())
            elif t in (0, 1, 0x11):     # nil, false, true
                p.k.append(None)
            else:
                raise ValueError(f"constant tag {t}")
        ups = [self.raw(3) for _ in range(self.size())]
        p.protos = [self.function() for _ in range(self.size())]
        lineinfo = self.raw(self.size())
        absl = dict((self.size(), self.size()) for _ in range(self.size()))
        p.locvars = [(self.string(), self.size(), self.size()) for _ in range(self.size())]
        p.upnames = [self.string() for _ in range(self.size())]
        if len(p.upnames) != len(ups):
            raise ValueError("stripped chunk")
        line, p.lines = p.linedefined, []
        for pc, d in enumerate(lineinfo):
            d = d - 256 if d > 127 else d
            line = absl[pc] if d == -128 else line + d
            p.lines.append(line)
        return p


def decode(i: int) -> tuple[str, int, int, int, int]:
    return OPNAMES[i & 0x7F], (i >> 7) & 0xFF, (i >> 15) & 1, (i >> 16) & 0xFF, (i >> 24) & 0xFF


_rt = L.LuaRuntime(register_eval=False, register_builtins=False, encoding=None)
_compile = _rt.execute(
    b'return function(s) local f, e = load(s, "=script", "t", {}) '
    b'if not f then return nil, e end return string.dump(f) end')


def compile_dump(src: bytes) -> Proto:
    r = _compile(src)
    blob, err = (r if isinstance(r, tuple) else (r, None))
    if blob is None:
        raise Refused("parse", (err or b"").decode(errors="replace"))
    rd = Reader(blob)
    rd.header()
    p = rd.function()
    if rd.i != len(blob):
        raise ValueError("trailing bytes after chunk")
    return p


def calibrate() -> None:
    """Pin the opcode numbering against Lua's own output."""
    p = compile_dump(b"x = y.z")
    ops = [decode(i)[0] for i in p.code]
    assert ops[:4] == ["VARARGPREP", "GETTABUP", "GETFIELD", "SETTABUP"], ops
    q = compile_dump(b"local t = {} t:m(1) return _ENV")
    ops = [decode(i)[0] for i in q.code]
    assert "SELF" in ops and "GETUPVAL" in ops and "NEWTABLE" in ops, ops


def check(src: bytes, grants: set[str]) -> set[str]:
    """Return the verbs the script calls; raise Refused on the first violation."""
    if src[:1] == b"\x1b":
        raise Refused("binary", "binary chunk")
    if len(src) > MAX_SOURCE:
        raise Refused("size", f"{len(src)} bytes")
    for n, c in enumerate(src):
        if c > 126 or (c < 32 and c not in (9, 10, 13)):
            raise Refused("bytes", f"byte {c} at offset {n}", src[:n].count(b"\n") + 1)
    for g in grants:
        if g not in VERBS:
            raise Refused("verb-unknown", f"grant {g}")
    ns_members: dict[str, set[str]] = {k: set(v) for k, v in LIBS.items()}
    for v in VERBS:
        ns_members.setdefault(v.split(".")[0], set())
    used: set[str] = set()
    _walk(compile_dump(src), grants, ns_members, used)
    unused = grants - used
    if unused:
        raise Refused("grant-unused", ",".join(sorted(unused)))
    return used


def _walk(p: Proto, grants: set[str], nsm: dict[str, set[str]], used: set[str]) -> None:
    env_up = {i for i, n in enumerate(p.upnames) if n == b"_ENV"}
    for name, _, _ in p.locvars:
        if name == b"_ENV":
            raise Refused("env", "local named _ENV")
    k = lambda idx: p.k[idx].decode() if isinstance(p.k[idx], bytes) else None
    for pc, ins in enumerate(p.code):
        op, a, kf, b, c = decode(ins)
        line = p.lines[pc]
        if op in ("GETUPVAL", "SETUPVAL") and b in env_up:
            raise Refused("env", "_ENV used as a value", line)
        if op == "SETTABUP":
            raise Refused("global-write", f"{k(b)}", line)
        if op in ("GETFIELD", "SELF") and (op == "GETFIELD" or kf):
            if k(c) in REFUSED_FIELDS:
                raise Refused("field-dump", k(c), line)
        if op == "SETFIELD" and k(b) in REFUSED_FIELDS:
            raise Refused("field-dump", k(b), line)
        if op != "GETTABUP":
            continue
        if b not in env_up:
            raise Refused("env", "table upvalue other than _ENV", line)
        name = k(c)
        if name in BASE:
            continue
        if name not in nsm:
            raise Refused("global", f"{name}", line)
        nxt = decode(p.code[pc + 1]) if pc + 1 < len(p.code) else None
        if nxt is None or nxt[0] != "GETFIELD" or nxt[1] != a or nxt[3] != a:
            if nxt and nxt[0] in ("GETTABLE", "SELF") and nxt[3] == a:
                raise Refused("ns-dynamic", f"{name}[...]", line)
            raise Refused("ns-value", f"{name} used as a value", line)
        member = k(nxt[4])
        if name in LIBS:
            if member in REFUSED_FIELDS:
                raise Refused("field-dump", f"{name}.{member}", line)
            if member not in nsm[name]:
                raise Refused("ns-member", f"{name}.{member}", line)
            continue
        verb = f"{name}.{member}"
        if verb not in VERBS:
            raise Refused("verb-unknown", verb, line)
        if verb not in grants:
            raise Refused("verb-ungranted", verb, line)
        used.add(verb)
    for q in p.protos:
        _walk(q, grants, nsm, used)


def header(src: bytes) -> tuple[set[str], str]:
    grants, expect = set(), "ok"
    for ln in src.decode("latin-1").splitlines()[:3]:
        if ln.startswith("-- grants:"):
            grants = {g.strip() for g in ln[10:].split(",") if g.strip()}
        elif ln.startswith("-- expect:"):
            expect = ln[10:].strip()
    return grants, expect


def main(corpus: pathlib.Path) -> int:
    calibrate()
    import lupa
    print(f"lupa {lupa.__version__}, {_rt.execute(b'return _VERSION').decode()}; "
          "opcode calibration ok")
    cases = [(f.name, f.read_bytes()) for f in sorted(corpus.glob("*.lua"))]
    cases.append(("inline_binary_chunk", b"\x1bLua\x54\x00"))
    # over 255 constants: Lua stops using GETTABUP for globals and reads _ENV
    # into a register (GETUPVAL), so the check must fail closed, never pass
    many = "local t = {" + ",".join(f'"k{i}"' for i in range(300)) + "}\n"
    cases.append(("inline_many_constants", f"-- expect: env\n{many}return tostring(t)\n".encode()))
    bad = 0
    for name, src in cases:
        grants, expect = header(src) if src[:1] != b"\x1b" else (set(), "binary")
        try:
            used = check(src, grants)
            got, why = "ok", ",".join(sorted(used)) or "-"
        except Refused as e:
            got, why = e.rule, str(e)
        mark = "PASS" if got == expect else "FAIL"
        bad += mark == "FAIL"
        print(f"{mark} {name:34} expect={expect:15} got={got:15} {why}")
    print(f"{len(cases)} cases, {bad} unexpected; sha256 of this checker "
          f"{hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest()[:16]}")
    return 1 if bad else 0


if __name__ == "__main__":
    here = pathlib.Path(__file__).resolve().parent
    sys.exit(main(pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else here / "corpus"))
