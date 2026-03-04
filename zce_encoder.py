"""
zce_encoder.py  —  ZCE Stream Encoder
======================================
Encodes programs to ZCE bitstreams compatible with zce_decoder_v2.h.

Architecture:
    Program (builder API)
        ↓
    Encoder (two-pass: measure → emit)
        ↓
    Emitter (bit-level: tokens, elias, raw32)
        ↓
    bytes  (valid ZCE stream)

Wire format (matches zce_decoder_v2.h exactly):
    elias(grammar_bits)          stream header
    [DEFINE_OP preambles]        teach decoder new ops before first use
    [instruction tokens + args]  the actual program
    [0-7 padding bits]           pad to byte boundary

Jump targets are bit offsets from grammar_start.
Label resolution is iterative (converges in ≤ 8 passes).

Extended ops (SUB, MUL, floats, etc.) are emitted as DEFINE_OP
before their first use on TARGET_STRIPPED. On TARGET_EXTENDED
they are assumed native and no DEFINE_OP is emitted.

Public API:
    from zce_encoder import Program, Encoder, Slots, TARGET_STRIPPED, TARGET_EXTENDED

    p = Program()
    p.set(0, 10)
    p.set(1, 32)
    p.add(2, 0, 1)          # slot[2] = 42
    stream = Encoder().encode(p)   # bytes, runs on zce_decoder_v2.h

See zce_tests.py for usage examples.
"""

import math
import struct
from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Optional


# ══════════════════════════════════════════════════════════════
# ELIAS GAMMA
# ══════════════════════════════════════════════════════════════

def elias_encode(v: int) -> str:
    """Encode non-negative integer v as Elias gamma bit string."""
    if v < 0:
        raise ValueError(f"elias_encode: v must be >= 0, got {v}")
    x = v + 1
    k = int(math.log2(x))
    return '0' * k + format(x, f'0{k+1}b')

def elias_decode(bits: str, pos: int) -> Tuple[int, int]:
    """Decode Elias gamma from bits[pos:]. Returns (value, new_pos)."""
    k = 0
    while pos < len(bits) and bits[pos] == '0':
        k += 1
        pos += 1
    if pos + k >= len(bits):
        raise ValueError(f"elias_decode: stream truncated at pos {pos}")
    x = int(bits[pos:pos + k + 1], 2)
    return x - 1, pos + k + 1

def elias_bits(v: int) -> int:
    """Number of bits to Elias-encode v."""
    return len(elias_encode(v))


# ══════════════════════════════════════════════════════════════
# TOKEN FORMAT
# ══════════════════════════════════════════════════════════════

def token_encode(n: int, depth: int = 1) -> str:
    """[N zeros][depth ones][0] — self-delimiting token."""
    if n < 0:
        raise ValueError(f"token_encode: n must be >= 0, got {n}")
    if depth < 1:
        raise ValueError(f"token_encode: depth must be >= 1, got {depth}")
    return '0' * n + '1' * depth + '0'

def token_size(n: int, depth: int = 1) -> int:
    """Bit size of a token."""
    return n + depth + 1


# ══════════════════════════════════════════════════════════════
# CONSTANTS  (must match zce_decoder_v2.h exactly)
# ══════════════════════════════════════════════════════════════

# Base tokens
TOK_SET       = 1
TOK_ADD       = 2
TOK_JIF       = 3   # immutable
TOK_JMP       = 4   # immutable
TOK_DEFINE_OP = 5   # immutable

# Argument types
ARG_ELIAS_PTR = 0
ARG_ELIAS_VAL = 1
ARG_RAW32     = 2

# Action codes
ACT_EXEC = 0
ACT_JIF  = 1
ACT_JMP  = 2
ACT_NOP  = 3

# Base executor op codes (action_param for ACT_EXEC)
OP_SET  = 1
OP_ADD  = 2

# Extended executor op codes
# These are action_param values — the decoder dispatches to
# zce_exec_extended(d, param, args) for anything > OP_ADD.
# The encoder emits DEFINE_OP with these param values.
# The decoder that wants to use them must implement the extension hook.
OP_SUB   = 3
OP_MUL   = 4
OP_DIV   = 5
OP_NEG   = 6
OP_FSET  = 10   # slots[dst] = raw32 float bits
OP_FADD  = 11
OP_FSUB  = 12
OP_FMUL  = 13
OP_FDIV  = 14
OP_FEXP  = 15
OP_FTANH = 16
OP_FSQRT = 17
OP_FABS  = 18
OP_FMAX  = 19
OP_FMIN   = 20
OP_FNEG   = 21
OP_FFLOOR = 22
OP_FCEIL  = 23
OP_FINT   = 24
OP_FFROM  = 25
OP_FCMP   = 26
OP_MOD    = 7
OP_AND    = 8
OP_OR     = 9
OP_PRINT  = 30
OP_PRINTF = 31

# Token slot assignments for stdlib ops
# N=1..5 are base. N=6..63 available for DEFINE_OP.
STDLIB_TOKEN: Dict[str, int] = {
    'SUB':   6,
    'MUL':   7,
    'DIV':   8,
    'NEG':   9,
    'FSET':  10,
    'FADD':  11,
    'FSUB':  12,
    'FMUL':  13,
    'FDIV':  14,
    'FEXP':  15,
    'FTANH': 16,
    'FSQRT': 17,
    'FABS':  18,
    'FMAX':  19,
    'FMIN':   20,
    'FNEG':   21,
    'FFLOOR': 22,
    'FCEIL':  23,
    'FINT':   24,
    'FFROM':  25,
    'FCMP':   26,
    'MOD':    27,
    'AND':    28,
    'OR':     29,
    'PRINT':  30,
    'PRINTF': 31,
}

# DEFINE_OP wire definitions for stdlib ops
# (n_args, [arg_types], action, action_param)
STDLIB_DEFS: Dict[str, tuple] = {
    'SUB':   (3, [ARG_ELIAS_PTR, ARG_ELIAS_PTR, ARG_ELIAS_PTR], ACT_EXEC, OP_SUB),
    'MUL':   (3, [ARG_ELIAS_PTR, ARG_ELIAS_PTR, ARG_ELIAS_PTR], ACT_EXEC, OP_MUL),
    'DIV':   (3, [ARG_ELIAS_PTR, ARG_ELIAS_PTR, ARG_ELIAS_PTR], ACT_EXEC, OP_DIV),
    'NEG':   (2, [ARG_ELIAS_PTR, ARG_ELIAS_PTR],                 ACT_EXEC, OP_NEG),
    'FSET':  (2, [ARG_ELIAS_PTR, ARG_RAW32],                     ACT_EXEC, OP_FSET),
    'FADD':  (3, [ARG_ELIAS_PTR, ARG_ELIAS_PTR, ARG_ELIAS_PTR], ACT_EXEC, OP_FADD),
    'FSUB':  (3, [ARG_ELIAS_PTR, ARG_ELIAS_PTR, ARG_ELIAS_PTR], ACT_EXEC, OP_FSUB),
    'FMUL':  (3, [ARG_ELIAS_PTR, ARG_ELIAS_PTR, ARG_ELIAS_PTR], ACT_EXEC, OP_FMUL),
    'FDIV':  (3, [ARG_ELIAS_PTR, ARG_ELIAS_PTR, ARG_ELIAS_PTR], ACT_EXEC, OP_FDIV),
    'FEXP':  (2, [ARG_ELIAS_PTR, ARG_ELIAS_PTR],                 ACT_EXEC, OP_FEXP),
    'FTANH': (2, [ARG_ELIAS_PTR, ARG_ELIAS_PTR],                 ACT_EXEC, OP_FTANH),
    'FSQRT': (2, [ARG_ELIAS_PTR, ARG_ELIAS_PTR],                 ACT_EXEC, OP_FSQRT),
    'FABS':  (2, [ARG_ELIAS_PTR, ARG_ELIAS_PTR],                 ACT_EXEC, OP_FABS),
    'FMAX':  (3, [ARG_ELIAS_PTR, ARG_ELIAS_PTR, ARG_ELIAS_PTR], ACT_EXEC, OP_FMAX),
    'FMIN':   (3, [ARG_ELIAS_PTR, ARG_ELIAS_PTR, ARG_ELIAS_PTR], ACT_EXEC, OP_FMIN),
    'FNEG':   (2, [ARG_ELIAS_PTR, ARG_ELIAS_PTR],                 ACT_EXEC, OP_FNEG),
    'FFLOOR': (2, [ARG_ELIAS_PTR, ARG_ELIAS_PTR],                 ACT_EXEC, OP_FFLOOR),
    'FCEIL':  (2, [ARG_ELIAS_PTR, ARG_ELIAS_PTR],                 ACT_EXEC, OP_FCEIL),
    'FINT':   (2, [ARG_ELIAS_PTR, ARG_ELIAS_PTR],                 ACT_EXEC, OP_FINT),
    'FFROM':  (2, [ARG_ELIAS_PTR, ARG_ELIAS_PTR],                 ACT_EXEC, OP_FFROM),
    'FCMP':   (3, [ARG_ELIAS_PTR, ARG_ELIAS_PTR, ARG_ELIAS_PTR], ACT_EXEC, OP_FCMP),
    'MOD':    (3, [ARG_ELIAS_PTR, ARG_ELIAS_PTR, ARG_ELIAS_PTR], ACT_EXEC, OP_MOD),
    'AND':    (3, [ARG_ELIAS_PTR, ARG_ELIAS_PTR, ARG_ELIAS_PTR], ACT_EXEC, OP_AND),
    'OR':     (3, [ARG_ELIAS_PTR, ARG_ELIAS_PTR, ARG_ELIAS_PTR], ACT_EXEC, OP_OR),
    'PRINT':  (1, [ARG_ELIAS_PTR],                                 ACT_EXEC, OP_PRINT),
    'PRINTF': (1, [ARG_ELIAS_PTR],                                 ACT_EXEC, OP_PRINTF),
}

# Immutable token slots that cannot be targeted by DEFINE_OP
_IMMUTABLE = {TOK_JIF, TOK_JMP, TOK_DEFINE_OP}


# ══════════════════════════════════════════════════════════════
# TARGET
# Controls which ops need DEFINE_OP preambles in the stream.
# ══════════════════════════════════════════════════════════════

class Target:
    """
    Defines what a decoder already knows natively.

    TARGET_STRIPPED: decoder knows only SET+ADD. Everything else needs
                     a DEFINE_OP preamble. Stream runs on any ZCE decoder.

    TARGET_EXTENDED: decoder also knows SUB, MUL, floats natively (via
                     zce_exec_extended). No DEFINE_OP needed for those.
                     Smaller streams, but requires the extended decoder build.
    """

    def __init__(self, name: str, extra_native: set = None):
        self.name = name
        # Base ops are always native. Extra_native adds more.
        self._native = {'SET', 'ADD', 'JIF', 'JMP', 'DEFINE_OP'}
        if extra_native:
            self._native |= extra_native

    def needs_define(self, op: str) -> bool:
        """True if op requires a DEFINE_OP preamble on this target."""
        return op not in self._native and op in STDLIB_TOKEN

    def token_for(self, op: str) -> int:
        """Return the token slot N for an op name."""
        base = {
            'SET': TOK_SET, 'ADD': TOK_ADD,
            'JIF': TOK_JIF, 'JMP': TOK_JMP,
            'DEFINE_OP': TOK_DEFINE_OP,
        }
        if op in base:
            return base[op]
        if op in STDLIB_TOKEN:
            return STDLIB_TOKEN[op]
        raise ValueError(f"Unknown op '{op}'. "
                         f"Known ops: {sorted(base) + sorted(STDLIB_TOKEN)}")


TARGET_STRIPPED = Target('stripped')
TARGET_EXTENDED = Target('extended', extra_native=set(STDLIB_TOKEN.keys()))


# ══════════════════════════════════════════════════════════════
# EMITTER
# Bit-level emission. Tracks current bit position.
# ══════════════════════════════════════════════════════════════

class Emitter:
    """
    Low-level bit emitter. Accumulates a bit string, finalizes to bytes.
    Stateful: tracks which ops have been DEFINE_OP'd so far.
    """

    def __init__(self, target: Target):
        self.target = target
        self._bits: str = ''
        self._defined: set = set()

    # ── primitive writers ──────────────────────────────────

    def _bits_append(self, b: str):
        self._bits += b

    def _emit_token(self, n: int, depth: int = 1):
        self._bits_append(token_encode(n, depth))

    def _emit_elias(self, v: int):
        self._bits_append(elias_encode(v))

    def _emit_raw32(self, v: int):
        self._bits_append(format(v & 0xFFFFFFFF, '032b'))

    # ── DEFINE_OP emission ─────────────────────────────────

    def emit_define_op(self, op: str):
        """Emit DEFINE_OP payload for op. Marks it as defined."""
        if op not in STDLIB_DEFS:
            raise ValueError(f"No DEFINE_OP definition for op '{op}'")
        n_args, arg_types, action, action_param = STDLIB_DEFS[op]
        tok_n = STDLIB_TOKEN[op]

        self._emit_token(TOK_DEFINE_OP)
        self._emit_elias(tok_n)          # target slot
        self._emit_elias(n_args - 1)     # n_args - 1 (decoder adds 1 back)
        for at in arg_types:
            self._emit_elias(at)
        self._emit_elias(action)
        if action == ACT_EXEC:
            self._emit_elias(action_param)

        self._defined.add(op)

    def ensure_defined(self, op: str):
        """Emit DEFINE_OP for op if this target needs it and it hasn't been emitted."""
        if self.target.needs_define(op) and op not in self._defined:
            self.emit_define_op(op)

    # ── instruction emission ───────────────────────────────

    def emit_instr(self, op: str, elias_args: list):
        """Emit token + Elias-encoded arguments."""
        self.ensure_defined(op)
        self._emit_token(self.target.token_for(op))
        for a in elias_args:
            self._emit_elias(a)

    def emit_instr_with_raw32(self, op: str, ptr_arg: int, raw_arg: int):
        """Emit token + Elias PTR arg + 32 raw bits (for FSET/floats)."""
        self.ensure_defined(op)
        self._emit_token(self.target.token_for(op))
        self._emit_elias(ptr_arg)
        self._emit_raw32(raw_arg)

    def emit_jif(self, cond_slot: int, target_offset: int):
        self._emit_token(TOK_JIF)
        self._emit_elias(cond_slot)
        self._emit_elias(target_offset)

    def emit_jmp(self, target_offset: int):
        self._emit_token(TOK_JMP)
        self._emit_elias(target_offset)

    # ── size measurement ───────────────────────────────────

    def measure_define_op(self, op: str) -> int:
        if op not in STDLIB_DEFS:
            return 0
        n_args, arg_types, action, action_param = STDLIB_DEFS[op]
        tok_n = STDLIB_TOKEN[op]
        size = token_size(TOK_DEFINE_OP)
        size += elias_bits(tok_n)
        size += elias_bits(n_args - 1)
        for at in arg_types:
            size += elias_bits(at)
        size += elias_bits(action)
        if action == ACT_EXEC:
            size += elias_bits(action_param)
        return size

    def measure_instr(self, op: str, elias_args: list) -> int:
        size = token_size(self.target.token_for(op))
        for a in elias_args:
            size += elias_bits(a)
        return size

    def measure_instr_with_raw32(self, op: str, ptr_arg: int) -> int:
        return token_size(self.target.token_for(op)) + elias_bits(ptr_arg) + 32

    def measure_jif(self, cond_slot: int, target_offset: int) -> int:
        return token_size(TOK_JIF) + elias_bits(cond_slot) + elias_bits(target_offset)

    def measure_jmp(self, target_offset: int) -> int:
        return token_size(TOK_JMP) + elias_bits(target_offset)

    # ── finalize ───────────────────────────────────────────

    def finalize(self) -> bytes:
        """Prepend elias(grammar_bits) header and pad to byte boundary."""
        grammar_bits = len(self._bits)
        full = elias_encode(grammar_bits) + self._bits
        while len(full) % 8:
            full += '0'
        result = bytearray()
        for i in range(0, len(full), 8):
            result.append(int(full[i:i + 8], 2))
        return bytes(result)

    @property
    def bit_pos(self) -> int:
        """Current bit position within grammar section (not counting header)."""
        return len(self._bits)


# ══════════════════════════════════════════════════════════════
# PROGRAM BUILDER
# High-level, human-friendly API for constructing ZCE programs.
# ══════════════════════════════════════════════════════════════

@dataclass
class _Op:
    name: str
    args: list
    meta: dict = field(default_factory=dict)


class Program:
    """
    High-level ZCE program builder.

    Labels are strings. Place them with .mark('name') before the
    instruction they should point to. Reference them in .jif/.jmp.

    Slot indices are plain integers. Use the Slots helper for
    named slot allocation.

    Example:
        p = Program()
        p.set(0, 0)          # i = 0
        p.set(1, 10)         # N = 10
        p.set(2, 1)          # one = 1
        s = Slots()
        p.mark('loop')
        p.sub(3, 1, 0)       # cond = N - i
        p.jif(3, 'exit')     # if cond == 0: exit
        p.add(0, 0, 2)       # i += 1
        p.jmp('loop')
        p.mark('exit')
        stream = Encoder().encode(p)
    """

    def __init__(self):
        self._ops: List[_Op] = []
        self._labels: Dict[str, int] = {}   # label → op index
        self._pending: List[str] = []        # labels waiting for next op

    def mark(self, label: str) -> 'Program':
        """Place a label at the next instruction."""
        self._pending.append(label)
        return self

    def _flush(self, idx: int):
        for lbl in self._pending:
            if lbl in self._labels:
                raise ValueError(f"Label '{lbl}' defined twice")
            self._labels[lbl] = idx
        self._pending.clear()

    def _add(self, name: str, args: list, **meta) -> 'Program':
        idx = len(self._ops)
        self._flush(idx)
        self._ops.append(_Op(name=name, args=args, meta=meta))
        return self

    # ── integer ops ───────────────────────────────────────

    def set(self, dst: int, val: int) -> 'Program':
        """slots[dst] = val  (immediate integer)"""
        return self._add('SET', [dst, val])

    def add(self, dst: int, a: int, b: int) -> 'Program':
        """slots[dst] = slots[a] + slots[b]"""
        return self._add('ADD', [dst, a, b])

    def sub(self, dst: int, a: int, b: int) -> 'Program':
        """slots[dst] = slots[a] - slots[b]"""
        return self._add('SUB', [dst, a, b])

    def mul(self, dst: int, a: int, b: int) -> 'Program':
        """slots[dst] = slots[a] * slots[b]"""
        return self._add('MUL', [dst, a, b])

    def div(self, dst: int, a: int, b: int) -> 'Program':
        """slots[dst] = slots[a] // slots[b]  (integer division)"""
        return self._add('DIV', [dst, a, b])

    def neg(self, dst: int, a: int) -> 'Program':
        """slots[dst] = -slots[a]"""
        return self._add('NEG', [dst, a])

    def mod(self, dst: int, a: int, b: int) -> 'Program':
        """slots[dst] = slots[a] % slots[b]  (mod by zero → 0)"""
        return self._add('MOD', [dst, a, b])

    def and_(self, dst: int, a: int, b: int) -> 'Program':
        """slots[dst] = slots[a] & slots[b]  (bitwise AND)"""
        return self._add('AND', [dst, a, b])

    def or_(self, dst: int, a: int, b: int) -> 'Program':
        """slots[dst] = slots[a] | slots[b]  (bitwise OR)"""
        return self._add('OR', [dst, a, b])

    # ── float ops ─────────────────────────────────────────

    def fset(self, dst: int, val: float) -> 'Program':
        """slots[dst] = float32(val) stored as raw bits"""
        raw = struct.unpack('I', struct.pack('f', float(val)))[0]
        return self._add('FSET', [dst, raw], raw32=True)

    def fadd(self, dst: int, a: int, b: int) -> 'Program':
        return self._add('FADD', [dst, a, b])

    def fsub(self, dst: int, a: int, b: int) -> 'Program':
        return self._add('FSUB', [dst, a, b])

    def fmul(self, dst: int, a: int, b: int) -> 'Program':
        return self._add('FMUL', [dst, a, b])

    def fdiv(self, dst: int, a: int, b: int) -> 'Program':
        return self._add('FDIV', [dst, a, b])

    def fexp(self, dst: int, a: int) -> 'Program':
        return self._add('FEXP', [dst, a])

    def ftanh(self, dst: int, a: int) -> 'Program':
        return self._add('FTANH', [dst, a])

    def fsqrt(self, dst: int, a: int) -> 'Program':
        return self._add('FSQRT', [dst, a])

    def fabs(self, dst: int, a: int) -> 'Program':
        return self._add('FABS', [dst, a])

    def fmax(self, dst: int, a: int, b: int) -> 'Program':
        return self._add('FMAX', [dst, a, b])

    def fmin(self, dst: int, a: int, b: int) -> 'Program':
        return self._add('FMIN', [dst, a, b])

    def fneg(self, dst: int, a: int) -> 'Program':
        """slots[dst] = -f32(slots[a])"""
        return self._add('FNEG', [dst, a])

    def ffloor(self, dst: int, a: int) -> 'Program':
        """slots[dst] = floorf(slots[a])"""
        return self._add('FFLOOR', [dst, a])

    def fceil(self, dst: int, a: int) -> 'Program':
        """slots[dst] = ceilf(slots[a])"""
        return self._add('FCEIL', [dst, a])

    def fint(self, dst: int, a: int) -> 'Program':
        """slots[dst] = (int64) f32(slots[a])  — float to integer"""
        return self._add('FINT', [dst, a])

    def ffrom(self, dst: int, a: int) -> 'Program':
        """slots[dst] = f32(slots[a])  — integer to float"""
        return self._add('FFROM', [dst, a])

    def fcmp(self, dst: int, a: int, b: int) -> 'Program':
        """slots[dst] = 1 if f32(slots[a]) > f32(slots[b]) else 0"""
        return self._add('FCMP', [dst, a, b])

    # ── control flow ──────────────────────────────────────

    def jif(self, cond: int, label: str) -> 'Program':
        """if slots[cond] == 0: jump to label"""
        return self._add('JIF', [cond], label=label)

    def jmp(self, label: str) -> 'Program':
        """Unconditional jump to label."""
        return self._add('JMP', [], label=label)

    # ── output ────────────────────────────────────────────

    def print_slot(self, slot: int) -> 'Program':
        """Print slots[slot] as decimal integer to stdout."""
        return self._add('PRINT', [slot])

    def printf_slot(self, slot: int) -> 'Program':
        """Print slots[slot] as float to stdout."""
        return self._add('PRINTF', [slot])

    def __len__(self) -> int:
        return len(self._ops)


# ══════════════════════════════════════════════════════════════
# SLOTS — named slot allocator
# ══════════════════════════════════════════════════════════════

class Slots:
    """
    Named slot allocator. Assigns sequential integers to names.

    Usage:
        s = Slots()
        i   = s['i']      # → 0
        acc = s['acc']    # → 1
        one = s['one']    # → 2
        # All subsequent s['i'] return 0 — same name, same slot.
    """

    def __init__(self, start: int = 0):
        self._map: Dict[str, int] = {}
        self._next = start

    def __getitem__(self, name: str) -> int:
        if name not in self._map:
            self._map[name] = self._next
            self._next += 1
        return self._map[name]

    def alloc(self, name: str) -> int:
        return self[name]

    def __repr__(self) -> str:
        return f"Slots({self._map})"


# ══════════════════════════════════════════════════════════════
# ENCODER
# Two-pass: measure bit offsets → emit correct jump targets.
# ══════════════════════════════════════════════════════════════

class Encoder:
    """
    Encodes a Program to a ZCE bitstream (bytes).

    Two-pass encoding:
      Pass 1 (repeated up to MAX_PASSES times):
        Simulate which DEFINE_OPs are emitted and when.
        Compute bit offset of every instruction.
        Jump target sizes depend on target offsets — iterate until stable.

      Pass 2:
        Emit actual bits using the stabilised offsets.

    The iteration converges because:
      - elias_bits() is monotone non-decreasing for increasing v
      - jump targets can only grow (never shrink) between passes
      - the grammar section is bounded
    """

    MAX_PASSES = 12

    def __init__(self, target: Target = TARGET_STRIPPED):
        self.target = target

    # ── internal: simulate which defines come before each instr ──

    def _plan_defines(self, ops: List[_Op]) -> Dict[int, List[str]]:
        """
        Returns {instr_idx: [op_names to DEFINE_OP before this instruction]}.
        Each op is defined at most once, at the first instruction that uses it.
        """
        defined: set = set()
        plan: Dict[int, List[str]] = {}
        for idx, op in enumerate(ops):
            if self.target.needs_define(op.name) and op.name not in defined:
                plan.setdefault(idx, []).append(op.name)
                defined.add(op.name)
        return plan

    # ── internal: measure one pass, return bit_offsets list ──

    def _measure(self, ops: List[_Op], plan: Dict[int, List[str]],
                 labels: Dict[str, int],
                 prev_offsets: Optional[List[int]]) -> List[int]:
        """
        Compute bit offset of each instruction in the grammar section.
        prev_offsets: offsets from previous pass (used to resolve jump targets).
        Returns list of length len(ops)+1 (last entry = end of all instructions).
        """
        em = Emitter(self.target)
        offsets = []
        pos = 0

        def label_offset(name: str, fallback: int) -> int:
            idx = labels.get(name)
            if idx is None:
                raise ValueError(f"Undefined label '{name}'")
            idx = min(idx, len(prev_offsets) - 1) if prev_offsets else fallback
            return prev_offsets[idx] if prev_offsets else fallback

        for idx, op in enumerate(ops):
            # DEFINE_OP preambles before this instruction
            for dop in plan.get(idx, []):
                pos += em.measure_define_op(dop)

            offsets.append(pos)

            if op.name == 'JIF':
                tgt = label_offset(op.meta['label'], pos) if prev_offsets else pos
                pos += em.measure_jif(op.args[0], tgt)
            elif op.name == 'JMP':
                tgt = label_offset(op.meta['label'], pos) if prev_offsets else pos
                pos += em.measure_jmp(tgt)
            elif op.meta.get('raw32'):
                pos += em.measure_instr_with_raw32(op.name, op.args[0])
            else:
                pos += em.measure_instr(op.name, op.args)

        offsets.append(pos)  # sentinel: end of grammar
        return offsets

    # ── public: encode ────────────────────────────────────

    def encode(self, program: Program) -> bytes:
        """Encode program to ZCE bytes."""
        ops = program._ops
        labels = dict(program._labels)

        # Flush any trailing labels (mark() calls after last instruction)
        program._flush(len(ops))
        labels.update(program._labels)

        if not ops:
            # Empty program: grammar_bits = 0, one byte (the elias header)
            return bytes([0x80])  # elias(0) = '1', padded to byte

        plan = self._plan_defines(ops)

        # ── Pass 1: iterate until bit offsets converge ──
        offsets = None
        for _ in range(self.MAX_PASSES):
            new_offsets = self._measure(ops, plan, labels, offsets)
            if new_offsets == offsets:
                break
            offsets = new_offsets

        assert offsets is not None

        # ── Pass 2: emit ──
        em = Emitter(self.target)

        def label_offset(name: str) -> int:
            idx = labels.get(name)
            if idx is None:
                raise ValueError(f"Undefined label '{name}'")
            return offsets[min(idx, len(offsets) - 1)]

        for idx, op in enumerate(ops):
            for dop in plan.get(idx, []):
                em.emit_define_op(dop)

            if op.name == 'JIF':
                em.emit_jif(op.args[0], label_offset(op.meta['label']))
            elif op.name == 'JMP':
                em.emit_jmp(label_offset(op.meta['label']))
            elif op.meta.get('raw32'):
                em.emit_instr_with_raw32(op.name, op.args[0], op.args[1])
            else:
                em.emit_instr(op.name, op.args)

        return em.finalize()

    def stats(self, program: Program) -> dict:
        """Return encoding statistics dict."""
        data = self.encode(program)
        ops_used = {op.name for op in program._ops}
        defines = {op for op in ops_used if self.target.needs_define(op)}
        return {
            'bytes':          len(data),
            'instructions':   len(program._ops),
            'ops_used':       ops_used,
            'defines_needed': defines,
            'target':         self.target.name,
        }


# ══════════════════════════════════════════════════════════════
# DISASSEMBLER
# Human-readable dump of a ZCE stream. For debugging.
# ══════════════════════════════════════════════════════════════

def disassemble(data: bytes) -> List[str]:
    """
    Decode a ZCE bitstream and return human-readable lines.
    Shows bit offset (relative to grammar_start) for each instruction.
    """
    bits = ''.join(format(b, '08b') for b in data)
    pos = 0

    grammar_bits, pos = elias_decode(bits, pos)
    grammar_start = pos
    grammar_end   = pos + grammar_bits

    lines = [
        f"; ZCE stream: {len(data)} bytes, grammar={grammar_bits} bits",
        f"; grammar_start={grammar_start}  grammar_end={grammar_end}",
    ]

    # Build name lookup
    tok_names: Dict[int, str] = {
        TOK_SET: 'SET', TOK_ADD: 'ADD', TOK_JIF: 'JIF',
        TOK_JMP: 'JMP', TOK_DEFINE_OP: 'DEFINE_OP',
    }
    for name, n in STDLIB_TOKEN.items():
        tok_names[n] = name

    # Track arg types for each defined token
    defined_args: Dict[int, List[int]] = {
        TOK_SET: [ARG_ELIAS_PTR, ARG_ELIAS_VAL],
        TOK_ADD: [ARG_ELIAS_PTR, ARG_ELIAS_PTR, ARG_ELIAS_PTR],
        TOK_JIF: [ARG_ELIAS_PTR, ARG_ELIAS_VAL],
        TOK_JMP: [ARG_ELIAS_VAL],
        TOK_DEFINE_OP: [],
    }

    def read_tok() -> Tuple[int, int]:
        nonlocal pos
        start = pos
        n = 0
        while pos < len(bits) and bits[pos] == '0':
            n += 1; pos += 1
        while pos < len(bits) and bits[pos] == '1':
            pos += 1  # depth
        if pos < len(bits):
            pos += 1  # terminator
        return n, start

    def read_elias() -> int:
        nonlocal pos
        v, pos = elias_decode(bits, pos)
        return v

    def read_raw32() -> int:
        nonlocal pos
        v = int(bits[pos:pos + 32], 2)
        pos += 32
        return v

    while pos < grammar_end:
        instr_start = pos
        n, _ = read_tok()
        if n == 0:
            continue

        offset = instr_start - grammar_start
        name = tok_names.get(n, f'N={n}')

        if n == TOK_DEFINE_OP:
            target  = read_elias()
            n_args  = read_elias() + 1
            atypes  = [read_elias() for _ in range(n_args)]
            action  = read_elias()
            ap      = read_elias() if action == ACT_EXEC else 0
            tname   = tok_names.get(target, f'N={target}')
            anames  = ['PTR', 'VAL', 'RAW32']
            atstr   = ','.join(anames[a] if a < len(anames) else str(a) for a in atypes)
            lines.append(
                f"  [{offset:5d}] DEFINE_OP  N={target}({tname})"
                f"  args=[{atstr}]  action={action}  param={ap}"
            )
            defined_args[target] = atypes

        elif n in defined_args:
            atypes = defined_args[n]
            arg_strs = []
            for at in atypes:
                if at == ARG_RAW32:
                    raw = read_raw32()
                    f32 = struct.unpack('f', struct.pack('I', raw))[0]
                    arg_strs.append(f"0x{raw:08x}({f32:.4f})")
                else:
                    arg_strs.append(str(read_elias()))
            lines.append(f"  [{offset:5d}] {name:<10s} {' '.join(arg_strs)}")

        else:
            lines.append(f"  [{offset:5d}] {name} (no DEFINE_OP — cannot decode args)")
            break

    return lines
