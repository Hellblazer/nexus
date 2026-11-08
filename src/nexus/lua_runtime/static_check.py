# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-242 Technical Design 3: the static check, read from Lua 5.4's own bytecode (nexus-mdbds.6).

The check never parses Lua. It compiles the source with Lua's own
``load(src, "=script", "t", {})`` in a runtime made for that one call (compiling runs
nothing), dumps the function with ``string.dump`` keeping the debug information, reads
the 5.4 binary chunk, and applies the rules to every function prototype. The checker and
the runtime therefore read the same grammar: a long bracket or a comment another parser
reads differently cannot hide a call, because the checker reads what Lua compiled.

It runs inside the run's worker (``python -I``, empty environment), before the script's
own runtime is built, so it imports only the standard library and ``lupa``. The grants
are an argument; this module reads no header (RDR-240 Technical Design 1 gives that job
to ``lua_runtime.header.parse`` alone). :func:`calibrate` pins the chunk layout and the
opcode numbering against Lua's own output, so a ``lupa`` whose Lua differs fails loudly
instead of being read wrong (RDR-242 assumption A4).

The allowlists of Technical Design 7 are module constants here, and the runtime of
nexus-mdbds.5 builds the script's environment from them, so the check and the
environment share one list.
"""
from __future__ import annotations

import struct
from collections.abc import Mapping
from dataclasses import dataclass, field

import lupa.lua54 as lua54

#: Lua 5.4 opcodes in ``lopcodes.h`` order. :func:`calibrate` pins the numbering.
OPNAMES: tuple[str, ...] = tuple(
    "MOVE LOADI LOADF LOADK LOADKX LOADFALSE LFALSESKIP LOADTRUE LOADNIL "
    "GETUPVAL SETUPVAL GETTABUP GETTABLE GETI GETFIELD SETTABUP SETTABLE SETI "
    "SETFIELD NEWTABLE SELF ADDI ADDK SUBK MULK MODK POWK DIVK IDIVK BANDK BORK "
    "BXORK SHRI SHLI ADD SUB MUL MOD POW DIV IDIV BAND BOR BXOR SHL SHR MMBIN "
    "MMBINI MMBINK UNM BNOT NOT LEN CONCAT CLOSE TBC JMP EQ LT LE EQK EQI LTI "
    "LEI GTI GEI TEST TESTSET CALL TAILCALL RETURN RETURN0 RETURN1 FORLOOP "
    "FORPREP TFORPREP TFORCALL TFORLOOP SETLIST CLOSURE VARARG VARARGPREP "
    "EXTRAARG".split()
)

#: Technical Design 7: the safe base functions (``pcall`` is the guarded one of item 11).
BASE_FUNCTIONS: frozenset[str] = frozenset({
    "assert", "error", "ipairs", "next", "pairs", "pcall", "select", "tonumber",
    "tostring", "type", "rawequal", "rawlen",
})

#: Technical Design 7: ``string`` without ``dump``, and all of ``table``, ``math`` and
#: ``utf8``. A test pins each set to the real library less ``dump``.
LIB_MEMBERS: Mapping[str, frozenset[str]] = {
    "string": frozenset({
        "byte", "char", "find", "format", "gmatch", "gsub", "len", "lower", "match",
        "pack", "packsize", "rep", "reverse", "sub", "unpack", "upper",
    }),
    "table": frozenset({"concat", "insert", "move", "pack", "remove", "sort", "unpack"}),
    "math": frozenset({
        "abs", "acos", "asin", "atan", "ceil", "cos", "deg", "exp", "floor", "fmod",
        "huge", "log", "max", "maxinteger", "min", "mininteger", "modf", "pi", "rad",
        "random", "randomseed", "sin", "sqrt", "tan", "tointeger", "type", "ult",
    }),
    "utf8": frozenset({"char", "charpattern", "codepoint", "codes", "len", "offset"}),
}

#: Technical Design 10: the version 1 verbs, all reads. The worker passes the names from
#: the parent's start frame; this default is for callers that run no worker.
VERBS_V1: frozenset[str] = frozenset({
    "memory.get", "memory.list", "memory.search", "transcripts.sessions",
    "git.diff_names", "git.diff_stat", "tables.get",
})

#: Field names refused on anything (rule ``field-dump``).
REFUSED_FIELDS: frozenset[str] = frozenset({"dump"})
#: Rule ``size``: the source bound in bytes.
MAX_SOURCE: int = 64 * 1024
#: Over this many constants in one function, Lua stops indexing with constant operands
#: (``GETTABUP``, ``GETFIELD``), and the rules below could no longer see a name. Such a
#: function is refused, never passed (Key Discoveries; reported under ``env``, the rule
#: the spike's case ``inline_many_constants`` recorded).
MAX_CONSTANTS: int = 255
#: Memory for the throwaway compile runtime. A 64 KB source compiles in far less.
COMPILE_MEMORY: int = 64 * 1024 * 1024

#: The rules of Technical Design 3, by their exact names.
RULES: frozenset[str] = frozenset({
    "binary", "bytes", "size", "parse", "global", "global-write", "env", "ns-value",
    "ns-dynamic", "ns-member", "field-dump", "verb-unknown", "verb-ungranted", "grant-unused",
})


class StaticCheckRefusal(Exception):
    """The script breaks rule *rule* (one of :data:`RULES`) at *line* (``None`` when the
    rule is about the whole source or the grants)."""

    def __init__(self, rule: str, detail: str, line: int | None = None) -> None:
        if rule not in RULES:
            raise ValueError(f"unknown static-check rule {rule!r}")
        self.rule, self.detail, self.line = rule, detail, line
        where = f" (line {line})" if line is not None else ""
        # The worker sends str(self) as the error frame's detail (rdr242-interfaces B).
        super().__init__(f"{rule}: {detail}{where}")


def _refusal(rule: str, line: int | None, detail: str) -> StaticCheckRefusal:
    return StaticCheckRefusal(rule, detail, line)


class ChunkFormatError(Exception):
    """The binary chunk is not the Lua 5.4 layout this reader was calibrated against.
    Never a verdict on the script: the run must not proceed."""


# ---- Lua 5.4 binary chunk reader (ldump.c layout) ----

_SIGNATURE = b"\x1bLua"
_VERSION = 0x54
_FORMAT = 0
_LUAC_DATA = b"\x19\x93\r\n\x1a\n"
_SIZES = b"\x04\x08\x08"  # Instruction, lua_Integer, lua_Number
_LUAC_INT = struct.pack("<q", 0x5678)
_LUAC_NUM = struct.pack("<d", 370.5)
_ABSLINEINFO = -0x80


@dataclass
class Proto:
    """One function prototype: what the rules read."""

    linedefined: int = 0
    code: list[int] = field(default_factory=list)
    k: list[bytes | None] = field(default_factory=list)
    upnames: list[bytes | None] = field(default_factory=list)
    protos: list[Proto] = field(default_factory=list)
    lines: list[int] = field(default_factory=list)
    locvars: list[bytes | None] = field(default_factory=list)


class _Reader:
    def __init__(self, blob: bytes) -> None:
        self.b, self.i = blob, 0

    def raw(self, n: int) -> bytes:
        if self.i + n > len(self.b):
            raise ChunkFormatError("truncated chunk")
        v = self.b[self.i:self.i + n]
        self.i += n
        return v

    def byte(self) -> int:
        return self.raw(1)[0]

    def size(self) -> int:  # big-endian 7-bit groups; the last byte carries 0x80
        x = 0
        for _ in range(10):
            c = self.byte()
            x = (x << 7) | (c & 0x7F)
            if c & 0x80:
                return x
        raise ChunkFormatError("size field too long")

    def string(self) -> bytes | None:
        n = self.size()
        return None if n == 0 else self.raw(n - 1)

    def header(self) -> None:
        if self.raw(4) != _SIGNATURE or self.byte() != _VERSION or self.byte() != _FORMAT:
            raise ChunkFormatError("not a Lua 5.4 chunk")
        if self.raw(6) != _LUAC_DATA or self.raw(3) != _SIZES:
            raise ChunkFormatError("unexpected chunk layout")
        if self.raw(8) != _LUAC_INT or self.raw(8) != _LUAC_NUM:
            raise ChunkFormatError("unexpected number format")
        self.byte()  # the main function's upvalue count

    def function(self) -> Proto:
        p = Proto()
        self.string()  # source
        p.linedefined = self.size()
        self.size()  # lastlinedefined
        self.raw(3)  # numparams, is_vararg, maxstacksize
        p.code = [int.from_bytes(self.raw(4), "little") for _ in range(self.size())]
        for _ in range(self.size()):
            tag = self.byte()
            if tag in (0x03, 0x13):  # integer, float
                p.k.append(self.raw(8))
            elif tag in (0x04, 0x14):  # short, long string
                p.k.append(self.string())
            elif tag in (0x00, 0x01, 0x11):  # nil, false, true
                p.k.append(None)
            else:
                raise ChunkFormatError(f"constant tag {tag}")
        n_up = self.size()
        self.raw(3 * n_up)  # instack, idx, kind
        p.protos = [self.function() for _ in range(self.size())]
        lineinfo = self.raw(self.size())
        absl = {}
        for _ in range(self.size()):
            pc = self.size()
            absl[pc] = self.size()
        p.locvars = []
        for _ in range(self.size()):
            p.locvars.append(self.string())
            self.size()  # startpc
            self.size()  # endpc
        p.upnames = [self.string() for _ in range(self.size())]
        if len(p.upnames) != n_up or len(lineinfo) != len(p.code):
            raise ChunkFormatError("stripped chunk: the check needs debug information")
        line = p.linedefined
        for pc, d in enumerate(lineinfo):
            d = d - 256 if d > 127 else d
            if d == _ABSLINEINFO:
                if pc not in absl:
                    raise ChunkFormatError("missing absolute line")
                line = absl[pc]
            else:
                line += d
            p.lines.append(line)
        return p


def read_chunk(blob: bytes) -> Proto:
    """The main prototype of a Lua 5.4 binary chunk dumped with debug information."""
    rd = _Reader(blob)
    rd.header()
    p = rd.function()
    if rd.i != len(blob):
        raise ChunkFormatError("trailing bytes after the chunk")
    return p


def _decode(ins: int) -> tuple[str, int, int, int, int]:
    """(opcode name, A, k, B, C) of an iABC instruction."""
    op = ins & 0x7F
    if op >= len(OPNAMES):
        raise ChunkFormatError(f"opcode {op} outside the table")
    return OPNAMES[op], (ins >> 7) & 0xFF, (ins >> 15) & 1, (ins >> 16) & 0xFF, (ins >> 24) & 0xFF


_COMPILE = (
    'return function(s) local f, e = load(s, "=script", "t", {}) '
    "if not f then return nil, e end return string.dump(f) end"
)


def _new_runtime() -> lua54.LuaRuntime:
    """A runtime for one compile; nothing from Python is registered in it."""
    return lua54.LuaRuntime(
        register_eval=False, register_builtins=False, encoding=None, max_memory=COMPILE_MEMORY,
    )


def _dump(rt: lua54.LuaRuntime, src: bytes) -> bytes:
    """Compile *src* in mode "t" with an empty environment and dump it; refuse ``parse``."""
    r = rt.execute(_COMPILE.encode("ascii"))(src)
    blob, err = r if isinstance(r, tuple) else (r, None)
    if blob is None:
        msg = (err or b"").decode("latin-1") if isinstance(err, bytes) else str(err)
        raise _refusal("parse", _line_of(msg), msg)
    return bytes(blob)


def _line_of(msg: str) -> int | None:
    # Lua's message reads 'script:<line>: ...' for the chunk name "=script".
    head, _, rest = msg.partition(":")
    num, _, _ = rest.partition(":")
    return int(num) if head == "script" and num.isdigit() else None


# ---- calibration ----

#: Compiled by :func:`calibrate`; its opcode sequence is pinned in :data:`_CALIBRATION_OPS`.
_CALIBRATION_SOURCE: bytes = (
    b"x = y.z\n"
    b"local t = {1, 2}\n"
    b"t.a = t[1]\n"
    b"t:m(1)\n"
    b"local function f() t.b = 1 return t.c end\n"
    b"for i = 1, 2 do t[i] = i + 1 end\n"
    b"return f, _ENV\n"
)
#: Read from Lua 5.4 as bundled by lupa 2.8 on 2026-11-07; the main function, then "|",
#: then the closure. Covers every opcode the rules read.
_CALIBRATION_OPS: tuple[str, ...] = (
    'VARARGPREP', 'GETTABUP', 'GETFIELD', 'SETTABUP', 'NEWTABLE', 'EXTRAARG',
    'LOADI', 'LOADI', 'SETLIST', 'GETI', 'SETFIELD', 'SELF',
    'LOADI', 'CALL', 'CLOSURE', 'LOADI', 'LOADI', 'LOADI',
    'FORPREP', 'ADDI', 'MMBINI', 'SETTABLE', 'FORLOOP', 'MOVE',
    'GETUPVAL', 'RETURN', 'RETURN', '|', 'SETTABUP', 'GETTABUP',
    'RETURN1', 'RETURN0',
)


def calibrate() -> None:
    """Fail with :class:`ChunkFormatError` unless the chunk layout and the opcode numbering
    are the ones this reader was written against."""
    p = read_chunk(_dump(_new_runtime(), _CALIBRATION_SOURCE))
    ops = tuple(_decode(i)[0] for i in p.code)
    inner = tuple(_decode(i)[0] for i in p.protos[0].code) if p.protos else ()
    got = ops + ("|",) + inner
    if got != _CALIBRATION_OPS:
        raise ChunkFormatError(f"opcode calibration failed: {got}")


# ---- the check ----

def check(source: bytes, grants: frozenset[str], verbs: frozenset[str] = VERBS_V1) -> frozenset[str]:
    """Return the verbs *source* names; raise :class:`StaticCheckRefusal` on the first
    violation of the rules.

    *grants* are the verbs the script declares (the worker has them from the parent's start
    frame, never from a header); *verbs* is every verb name the run may know. A grant outside
    *verbs* is ``verb-unknown``, and so is a call to one.
    """
    if source[:1] == b"\x1b":
        raise _refusal("binary", None, "binary chunk")
    if len(source) > MAX_SOURCE:
        raise _refusal("size", None, f"{len(source)} bytes, over {MAX_SOURCE}")
    for n, c in enumerate(source):
        if c > 126 or (c < 32 and c not in (9, 10, 13)):
            raise _refusal("bytes", source.count(b"\n", 0, n) + 1, f"byte {c} at offset {n}")
    for g in sorted(grants):
        if g not in verbs:
            raise _refusal("verb-unknown", None, f"grant {g}")
    namespaces = {v.split(".", 1)[0] for v in verbs}
    used: set[str] = set()
    _walk(read_chunk(_dump(_new_runtime(), source)), grants, verbs, namespaces, used)
    unused = sorted(grants - used)
    if unused:
        raise _refusal("grant-unused", None, ", ".join(unused))
    return frozenset(used)


def _walk(p: Proto, grants: frozenset[str], verbs: frozenset[str],
          namespaces: set[str], used: set[str]) -> None:
    if len(p.k) > MAX_CONSTANTS:
        raise _refusal("env", p.linedefined or 1, f"{len(p.k)} constants in one function, over {MAX_CONSTANTS}")
    for name in p.locvars:
        if name == b"_ENV":
            raise _refusal("env", p.linedefined or None, "a local or parameter named _ENV")
    env_up = {i for i, n in enumerate(p.upnames) if n == b"_ENV"}

    def k(idx: int) -> str | None:
        v = p.k[idx] if idx < len(p.k) else None
        return v.decode("latin-1") if isinstance(v, bytes) else None

    for pc, ins in enumerate(p.code):
        op, a, kf, b, c = _decode(ins)
        line = p.lines[pc]
        if op in ("GETUPVAL", "SETUPVAL") and b in env_up:
            raise _refusal("env", line, "_ENV used as a value")
        if op == "SETTABUP":
            # SETTABUP A B C: UpValue[A][K[B]] := RK(C)
            if a in env_up:
                raise _refusal("global-write", line, f"{k(b)}")
            if k(b) in REFUSED_FIELDS:
                raise _refusal("field-dump", line, f"{k(b)}")
            continue
        if op == "GETFIELD" or (op == "SELF" and kf):
            if k(c) in REFUSED_FIELDS:
                raise _refusal("field-dump", line, f"{k(c)}")
        if op == "SETFIELD" and k(b) in REFUSED_FIELDS:
            raise _refusal("field-dump", line, f"{k(b)}")
        if op != "GETTABUP":
            continue
        # GETTABUP A B C: R[A] := UpValue[B][K[C]]. An upvalue other than _ENV is an
        # ordinary table a closure captured: a field read, not a global.
        name = k(c)
        if b not in env_up:
            if name in REFUSED_FIELDS:
                raise _refusal("field-dump", line, f"{name}")
            continue
        if name in BASE_FUNCTIONS:
            continue
        if name not in LIB_MEMBERS and name not in namespaces:
            raise _refusal("global", line, f"{name}")
        nxt = _decode(p.code[pc + 1]) if pc + 1 < len(p.code) else None
        if nxt is None or nxt[0] != "GETFIELD" or nxt[1] != a or nxt[3] != a:
            if nxt is not None and nxt[3] == a and (nxt[0] == "GETTABLE" or (nxt[0] == "SELF" and not nxt[2])):
                raise _refusal("ns-dynamic", line, f"{name}[...]")
            raise _refusal("ns-value", line, f"{name} used other than as {name}.member")
        member = k(nxt[4])
        if member is None:
            raise _refusal("ns-dynamic", line, f"{name}[...]")
        if name in LIB_MEMBERS:
            if member in REFUSED_FIELDS:
                raise _refusal("field-dump", line, f"{name}.{member}")
            if member not in LIB_MEMBERS[name]:
                raise _refusal("ns-member", line, f"{name}.{member}")
            continue
        verb = f"{name}.{member}"
        if verb not in verbs:
            raise _refusal("verb-unknown", line, verb)
        if verb not in grants:
            raise _refusal("verb-ungranted", line, verb)
        used.add(verb)
    for q in p.protos:
        _walk(q, grants, verbs, namespaces, used)
