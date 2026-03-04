# ZCE Encoder — Complete Documentation

**File:** `zce_encoder.py`  
**Language:** Python 3.8+  
**Depends on:** nothing (stdlib only: math, struct, dataclasses)  
**Produces:** ZCE bitstreams compatible with `zce_decoder_v2.h`

---

## What This Is

The encoder takes a ZCE program written in a high-level Python API and produces a binary ZCE stream — a sequence of bytes that `zce_decoder_v2.h` can execute. It handles all the low-level details: Elias gamma encoding of integers, token format encoding, DEFINE_OP preamble emission, and jump target resolution.

The encoder is not frozen. It can be extended, improved, and modified freely. Only the decoder is frozen. This is the correct relationship — the encoder is a tool that produces artifacts for the frozen primitive.

---

## Architecture

The encoder has four distinct layers. Each does one thing and nothing else.

```
Program (builder API)
    ↓  human-friendly operations: set, add, sub, jif, mark...
    
Target (platform definition)
    ↓  which ops need DEFINE_OP preambles vs are native
    
Emitter (bit-level emission)
    ↓  tokens, elias gamma, raw32, DEFINE_OP payloads
    ↓  tracks which ops have been defined in this stream
    
finalize() → bytes
```

**Why these four layers?**

Each layer has a single responsibility. The Program builder knows nothing about bits. The Emitter knows nothing about program structure or labels. The Target knows nothing about encoding. Keeping them separate means:

- You can change how jump targets are resolved without touching bit emission
- You can add new ops to the stdlib without touching the emitter
- You can write a different frontend (LLVM IR → Program, assembly text → Program) without touching any encoding logic

---

## Layer 1: Elias Gamma

Everything in ZCE is encoded with Elias gamma. Understanding it is essential.

### Encoding

```python
def elias_encode(v: int) -> str:
    x = v + 1
    k = int(math.log2(x))
    return '0' * k + format(x, f'0{k+1}b')
```

Step by step for v=5:
- x = 5 + 1 = 6
- k = floor(log2(6)) = 2
- Write 2 zeros: `00`
- Write 6 in 3-bit binary: `110`
- Result: `00110`

### Decoding

```python
def elias_decode(bits: str, pos: int) -> Tuple[int, int]:
    k = 0
    while bits[pos] == '0':
        k += 1; pos += 1
    x = int(bits[pos:pos + k + 1], 2)
    return x - 1, pos + k + 1
```

### Why it matters

For a program that uses slot indices 0–10 and small values, Elias encoding saves enormous amounts of space compared to fixed-width encoding:

| Value | Fixed 16-bit | Elias | Savings |
|-------|-------------|-------|---------|
| 0 | 16 bits | 1 bit | 94% |
| 1 | 16 bits | 3 bits | 81% |
| 5 | 16 bits | 5 bits | 69% |
| 10 | 16 bits | 7 bits | 56% |
| 100 | 16 bits | 13 bits | 19% |

Most programs use small slot indices. Elias encoding makes ZCE streams compact by design, not by compression.

---

## Layer 2: Token Encoding

```python
def token_encode(n: int, depth: int = 1) -> str:
    return '0' * n + '1' * depth + '0'
```

Token N with depth D encodes as N zeros, D ones, one zero. The encoder always uses depth=1. Token N=1 (SET) becomes `010`. Token N=6 (SUB) becomes `0000010`.

`token_size(n, depth)` returns the bit length: `n + depth + 1`.

---

## Layer 3: The Emitter

The Emitter is the bit accumulation engine. It maintains a string of bits internally and provides methods to append tokens, Elias values, and raw 32-bit fields. It tracks which ops have been DEFINE_OP'd in the current stream.

### Key methods

```python
emit_define_op(op: str)
    # Emit the full DEFINE_OP wire payload for a named op.
    # Marks the op as defined in this stream.

ensure_defined(op: str)
    # If this target needs a DEFINE_OP for op, and it hasn't been
    # emitted yet, emit it now. Idempotent — safe to call many times.

emit_instr(op: str, elias_args: list)
    # Emit token + Elias-encoded arguments.
    # Calls ensure_defined first.

emit_instr_with_raw32(op: str, ptr_arg: int, raw_arg: int)
    # Emit token + one Elias arg + 32 raw bits.
    # Used for FSET (float literal).

emit_jif(cond_slot: int, target_offset: int)
emit_jmp(target_offset: int)
    # Emit branch instructions with their arguments.

finalize() -> bytes
    # Prepend elias(grammar_bits) header.
    # Pad to byte boundary.
    # Return complete stream as bytes.
```

### Measurement methods

Every emit method has a corresponding measurement method that returns the bit count that would be emitted — without actually emitting anything. These are used in Pass 1 of the encoder (layout phase).

```python
measure_define_op(op: str) -> int
measure_instr(op: str, elias_args: list) -> int
measure_instr_with_raw32(op: str, ptr_arg: int) -> int
measure_jif(cond_slot: int, target_offset: int) -> int
measure_jmp(target_offset: int) -> int
```

Why do we need these? Because jump targets are bit offsets — to know where instruction N is, you need to know the exact bit size of everything before it. The measurement methods let us compute layouts without actually emitting bits.

### DEFINE_OP emission

When the emitter sees `emit_define_op('SUB')`, it looks up `STDLIB_DEFS['SUB']` which contains:

```python
(3, [ARG_ELIAS_PTR, ARG_ELIAS_PTR, ARG_ELIAS_PTR], ACT_EXEC, OP_SUB)
# n_args=3, arg_types=[PTR,PTR,PTR], action=EXEC, action_param=3
```

And `STDLIB_TOKEN['SUB']` which gives the token slot number (6).

It then emits:
```
tok(5,1)          DEFINE_OP token
elias(6)          target = token 6
elias(2)          n_args - 1 = 2
elias(0)          arg_type[0] = ARG_ELIAS_PTR
elias(0)          arg_type[1] = ARG_ELIAS_PTR
elias(0)          arg_type[2] = ARG_ELIAS_PTR
elias(0)          action = ACT_EXEC
elias(3)          action_param = OP_SUB = 3
```

This teaches any ZCE decoder that token N=6 means: read three Elias slot pointers, call exec_ext with param=3.

---

## Layer 4: Target

The Target defines what a decoder already knows natively. The encoder uses this to decide which ops need DEFINE_OP preambles.

```python
TARGET_STRIPPED = Target('stripped')
```
Only SET and ADD are native. Everything else (SUB, MUL, floats, all of it) needs a DEFINE_OP before first use. This stream runs on **any** ZCE decoder — the absolute minimum decoder, the one with no extensions installed, even a decoder running on a device that only implements the 5 base tokens.

```python
TARGET_EXTENDED = Target('extended', extra_native=set(STDLIB_TOKEN.keys()))
```
All stdlib ops are native. No DEFINE_OP emitted for them. Smaller streams, but requires a decoder that has `zce_stdlib_exec` installed as its `exec_ext`. Use this when you control both the encoder and decoder.

**Why two targets?**

The same program produces different streams for different targets. On TARGET_STRIPPED, a program with SUB gets a DEFINE_OP preamble that takes ~30-40 bits before the first SUB instruction. On TARGET_EXTENDED, no preamble needed. For a program that uses SUB once, this is a 30-bit difference. For a program that uses SUB a thousand times, same 30-bit difference — DEFINE_OP is emitted once per op per stream regardless of how many times the op is used.

---

## The STDLIB_TOKEN and STDLIB_DEFS Tables

These two tables are the contract between the encoder and the decoder. They must match what `zce_stdlib.h` implements exactly.

### STDLIB_TOKEN — token slot assignments

```python
STDLIB_TOKEN = {
    'SUB':   6,   # N=6 zeros in the token header
    'MUL':   7,
    'DIV':   8,
    'NEG':   9,
    'MOD':   27,
    'AND':   28,
    'OR':    29,
    'FSET':  10,
    'FADD':  11,
    ...
    'PRINT': 30,
}
```

These token slot numbers can be anything from 6 to 63. They determine how many zeros appear in the token header for each op. N=6 costs 8 bits per token. N=30 costs 32 bits per token. Common ops get small N values (SUB=6, MUL=7, all floats in 10-26) to keep stream size down.

### STDLIB_DEFS — DEFINE_OP wire definitions

```python
STDLIB_DEFS = {
    'SUB': (3, [ARG_ELIAS_PTR, ARG_ELIAS_PTR, ARG_ELIAS_PTR], ACT_EXEC, OP_SUB),
    #       ^   ^arg types                                       ^action  ^action_param
    #       n_args
}
```

Each entry says: when DEFINE_OP for this op is emitted, what are its argument types, what action does it register, and what action_param does `exec_ext` receive.

### The op code / token slot distinction

This is the most important thing to understand about the encoder's table structure.

**Token slot** (STDLIB_TOKEN): the N in the token format. Wire-level. Determines how many zeros appear before the token's one-bits. Small N = smaller token in the stream.

**Op code** (action_param in STDLIB_DEFS): the integer `exec_ext` receives when the op executes. `OP_SUB = 3`. The C stdlib switches on this value.

These are completely independent. SUB has token slot 6 and op code 3. They happen to be different numbers. A future op could have token slot 15 and op code 15 — they could be equal by coincidence. What matters is that the encoder's `OP_SUB = 3` matches `zce_stdlib.h`'s `#define ZCE_OP_SUB 3`. The token slot number doesn't appear in the C code at all.

---

## The Program Builder

The Program class is the user-facing API. You write programs with method calls, not bit manipulation.

```python
p = Program()
p.set(0, 42)       # slots[0] = 42
p.set(1, 58)       # slots[1] = 58
p.add(2, 0, 1)     # slots[2] = slots[0] + slots[1]  →  100
```

### Integer operations

```python
p.set(dst, val)          # slots[dst] = immediate integer
p.add(dst, a, b)         # slots[dst] = slots[a] + slots[b]
p.sub(dst, a, b)         # slots[dst] = slots[a] - slots[b]
p.mul(dst, a, b)         # slots[dst] = slots[a] * slots[b]
p.div(dst, a, b)         # slots[dst] = slots[a] // slots[b]
p.neg(dst, a)            # slots[dst] = -slots[a]
p.mod(dst, a, b)         # slots[dst] = slots[a] % slots[b]
p.and_(dst, a, b)        # slots[dst] = slots[a] & slots[b]  (bitwise)
p.or_(dst, a, b)         # slots[dst] = slots[a] | slots[b]  (bitwise)
```

### Float operations

Floats are stored as raw IEEE 754 bit patterns in slots. The decoder's stdlib handles the actual float arithmetic. The encoder handles the literal encoding (fset uses ARG_RAW32 to embed float bits directly in the stream).

```python
p.fset(dst, 3.14)        # slots[dst] = float32(3.14) as raw bits
p.fadd(dst, a, b)        # float add
p.fsub(dst, a, b)        # float subtract
p.fmul(dst, a, b)        # float multiply
p.fdiv(dst, a, b)        # float divide
p.fexp(dst, a)           # e^x
p.ftanh(dst, a)          # tanh(x)  — key ML activation
p.fsqrt(dst, a)          # sqrt(x)
p.fabs(dst, a)           # |x|
p.fmax(dst, a, b)        # max(x, y)
p.fmin(dst, a, b)        # min(x, y)
p.fneg(dst, a)           # -x (float)
p.ffloor(dst, a)         # floor(x)
p.fceil(dst, a)          # ceil(x)
p.fint(dst, a)           # float → integer truncation
p.ffrom(dst, a)          # integer → float
p.fcmp(dst, a, b)        # 1 if slots[a] > slots[b] as floats, else 0
```

### Control flow

```python
p.mark('label')          # place a label at the next instruction
p.jif(cond, 'label')     # if slots[cond] == 0: jump to label
p.jmp('label')           # unconditional jump to label
```

Labels are strings. Place them with `mark()` before the instruction they should target. Use them in `jif` and `jmp`. The encoder resolves labels to bit offsets automatically.

A label placed after the last instruction (as a `mark()` call with no following instruction) resolves to the end of the grammar section, which is how you implement exit conditions.

**Duplicate labels raise ValueError.** This is a deliberate design choice — duplicate labels are almost always bugs, and a silent overwrite would produce incorrect jump targets.

### I/O

```python
p.print_slot(slot)       # print slots[slot] as integer
p.printf_slot(slot)      # print slots[slot] as float
```

### The Slots helper

```python
s = Slots()
i   = s['i']     # → 0, allocates slot 0 for 'i'
acc = s['acc']   # → 1, allocates slot 1 for 'acc'
one = s['one']   # → 2, allocates slot 2 for 'one'
i   = s['i']     # → 0, same name returns same slot
```

Named slot allocation. Instead of tracking slot numbers manually, use names. The Slots object assigns sequential integers and returns the same integer for the same name. Makes programs readable and prevents slot collisions.

---

## The Encoder — Two-Pass Encoding

The `Encoder` class takes a `Program` and produces bytes. The core challenge: jump targets are bit offsets. To know the bit offset of instruction N, you need to know the exact bit size of everything before instruction N, including all DEFINE_OP preambles. But DEFINE_OP preambles appear just before the first use of each op, which depends on program structure.

The encoder solves this with two passes.

### Pass 1: Layout

Before emitting any bits, the encoder simulates the emission to compute bit offsets.

Step 1: Determine which DEFINE_OPs are needed and when.

```python
def _plan_defines(self, ops):
    # Returns {instr_idx: [op_names to define before this instruction]}
    defined = set()
    plan = {}
    for idx, op in enumerate(ops):
        if target.needs_define(op.name) and op.name not in defined:
            plan.setdefault(idx, []).append(op.name)
            defined.add(op.name)
    return plan
```

Step 2: Measure bit offset of each instruction, iterating until stable.

```python
def _measure(self, ops, plan, labels, prev_offsets):
    pos = 0
    offsets = []
    for idx, op in enumerate(ops):
        for dop in plan.get(idx, []):
            pos += emitter.measure_define_op(dop)
        offsets.append(pos)
        if op.name == 'JIF':
            target = prev_offsets[labels[op.meta['label']]]
            pos += emitter.measure_jif(op.args[0], target)
        elif op.name == 'JMP':
            ...
        else:
            pos += emitter.measure_instr(op.name, op.args)
    offsets.append(pos)  # sentinel: end of grammar
    return offsets
```

### Why iteration is needed

Jump target offsets appear inside the jump instruction arguments. Those arguments are Elias-encoded. The bit size of an Elias-encoded value depends on the value itself. If a jump target is 500 bits, it needs 9 bits to encode. If the same jump target is 1000 bits (because some instruction grew), it needs 11 bits to encode. But if the encoding of the jump target is larger, then everything after the jump instruction is shifted, which changes other jump targets...

This is a classic chicken-and-egg problem in assemblers. The standard solution is iterative: compute offsets assuming small jump sizes, update jump targets, recompute offsets, repeat until stable. The encoder does this with `MAX_PASSES = 12` iterations.

**Does it always converge?**

Yes. The proof is simple: Elias-encoded values are monotone non-decreasing — making a value larger never makes its encoding shorter. Jump targets can only grow (never shrink) between iterations. And since the grammar section is bounded, targets are bounded. Convergence is guaranteed, typically in 2-3 iterations for any real program.

### Pass 2: Emit

Once offsets are stable, Pass 2 is straightforward — emit exactly what Pass 1 computed, using the stable offsets for jump targets.

---

## Example: Compiling a Loop

```python
s = Slots()
p = Program()
p.set(s['i'], 0)
p.set(s['N'], 10)
p.set(s['one'], 1)
p.mark('loop')
p.sub(s['cond'], s['N'], s['i'])    # cond = N - i
p.jif(s['cond'], 'exit')            # if cond==0: exit
p.add(s['i'], s['i'], s['one'])     # i += 1
p.jmp('loop')                       # goto loop
p.mark('exit')

stream = Encoder().encode(p)
```

What the encoder produces:

1. `DEFINE_OP N=6 (SUB)` — before first use of SUB
2. `SET 0 0` — i = 0
3. `SET 1 10` — N = 10
4. `SET 2 1` — one = 1
5. `SUB 3 1 0` — cond = N - i  ← this is instruction at bit offset X
6. `JIF 3 Y` — if cond==0 jump to bit offset Y (the exit label)
7. `ADD 0 0 2` — i += one
8. `JMP X` — jump back to SUB instruction
9. (end of stream) ← exit label resolves here

The encoder computes X and Y exactly during the layout pass. Y is the bit offset one past the last instruction — the exit point. X is the bit offset of the SUB instruction.

---

## The Disassembler

`disassemble(data: bytes) -> List[str]`

Takes ZCE bytes and returns human-readable lines. Used for debugging, verifying encoder output, and understanding what a stream actually contains.

```
; ZCE stream: 12 bytes, grammar=78 bits
; grammar_start=9  grammar_end=87
  [    0] DEFINE_OP  N=6(SUB)  args=[PTR,PTR,PTR]  action=0  param=3
  [   40] SET        0 0
  [   44] SET        1 10
  [   48] SET        2 1
  [   52] SUB        3 1 0
  [   57] JIF        3 78
  [   63] ADD        0 0 2
  [   68] JMP        52
```

The `[offset]` column shows bit offset relative to grammar_start. JIF at offset 57 targets bit 78 — but grammar_end is 87, so 78 is valid. JMP at offset 68 targets 52 — that's the SUB instruction. This is the loop structure.

The disassembler also shows float values when it encounters ARG_RAW32 arguments, both as hex and as the decoded float.

---

## The Stats Method

```python
stats = Encoder().stats(program)
# Returns:
# {
#   'bytes': 24,
#   'instructions': 8,
#   'ops_used': {'SET', 'ADD', 'SUB', 'JIF', 'JMP'},
#   'defines_needed': {'SUB'},
#   'target': 'stripped'
# }
```

Useful for understanding stream size, checking which DEFINE_OPs will be emitted, and comparing TARGET_STRIPPED vs TARGET_EXTENDED for a given program.

---

## The Python Simulator

The test file (`zce_tests.py`) contains a Python reimplementation of the ZCE decoder. This is not part of the encoder product — it's a test tool.

**Why does it exist?**

Testing the encoder without the Python simulator requires: writing the stream to a file, compiling the C harness, running it, parsing the output. That's 3-4 steps with a build dependency.

The Python simulator lets you test the encoder instantly in the same Python process. You encode a program, run it through the simulator, assert slot values. No C compiler needed. This makes it practical to run the test suite constantly during development.

**What does it prove?**

The Python simulator proves the encoder is logically correct — that the bit patterns it produces decode to the intended slot values. It does not prove wire format compatibility with the C decoder.

The C integration tests (using `zce_harness`) prove wire format compatibility. Both kinds of tests are necessary.

**The Python simulator is not a product.** It's not an alternative decoder, not something users should run programs on, not a reference implementation. It is purely a test acceleration tool. The canonical decoder is `zce_decoder_v2.h`.

---

## The C Harness

`zce_harness.c` is a small C program that:
1. Reads ZCE bytes from stdin
2. Runs them through `zce_decoder_v2.h` with `zce_stdlib_exec` installed
3. Prints all non-zero slot values to stdout

The test suite compiles this once and uses it to run C integration tests. It is the bridge between Python tests and the real C decoder.

To compile:

```bash
gcc -O2 -o zce_harness zce_harness.c -lm
```

Like the Python simulator, this is a test tool, not a product. Real applications include `zce_decoder_v2.h` directly.

---

## Complete Op Code Reference

The following table is the ground truth for all three components: `zce_encoder.py`, `zce_stdlib.h`, and `zce_harness.c`. All three must agree on op codes exactly.

| Op name | Token slot | Op code | Args | Description |
|---------|-----------|---------|------|-------------|
| SET | 1 (base) | 1 | PTR, VAL | slots[dst] = immediate |
| ADD | 2 (base) | 2 | PTR, PTR, PTR | integer add |
| JIF | 3 (base) | — | PTR, VAL | conditional branch |
| JMP | 4 (base) | — | VAL | unconditional branch |
| DEFINE_OP | 5 (base) | — | — | define new token |
| SUB | 6 | 3 | PTR, PTR, PTR | integer subtract |
| MUL | 7 | 4 | PTR, PTR, PTR | integer multiply |
| DIV | 8 | 5 | PTR, PTR, PTR | integer divide (÷0→0) |
| NEG | 9 | 6 | PTR, PTR | integer negate |
| FSET | 10 | 10 | PTR, RAW32 | load float literal |
| FADD | 11 | 11 | PTR, PTR, PTR | float add |
| FSUB | 12 | 12 | PTR, PTR, PTR | float subtract |
| FMUL | 13 | 13 | PTR, PTR, PTR | float multiply |
| FDIV | 14 | 14 | PTR, PTR, PTR | float divide (÷0→0) |
| FEXP | 15 | 15 | PTR, PTR | e^x (clamped at 88.0) |
| FTANH | 16 | 16 | PTR, PTR | tanh(x) |
| FSQRT | 17 | 17 | PTR, PTR | sqrt(x) |
| FABS | 18 | 18 | PTR, PTR | |x| |
| FMAX | 19 | 19 | PTR, PTR, PTR | max(x,y) |
| FMIN | 20 | 20 | PTR, PTR, PTR | min(x,y) |
| FNEG | 21 | 21 | PTR, PTR | float negate |
| FFLOOR | 22 | 22 | PTR, PTR | floor(x) |
| FCEIL | 23 | 23 | PTR, PTR | ceil(x) |
| FINT | 24 | 24 | PTR, PTR | float→integer |
| FFROM | 25 | 25 | PTR, PTR | integer→float |
| FCMP | 26 | 26 | PTR, PTR, PTR | 1 if x>y else 0 |
| MOD | 27 | 7 | PTR, PTR, PTR | integer modulo (÷0→0) |
| AND | 28 | 8 | PTR, PTR, PTR | bitwise AND |
| OR | 29 | 9 | PTR, PTR, PTR | bitwise OR |
| PRINT | 30 | 30 | PTR | print integer |
| PRINTF | 31 | 31 | PTR | print float |

---

## Test Suite Structure

The test suite (`zce_tests.py`) has five sections, each testing a different thing.

### Unit: Elias Gamma

Tests `elias_encode` and `elias_decode` independently. Checks known values (`elias_encode(0) == '1'`), roundtrips for all values 0–127, bit count formula, and error handling on negative input.

**What it proves:** The fundamental encoding primitive is correct. If Elias is wrong, everything above it is wrong. Testing it independently means failures here are unambiguous.

### Unit: Token Format

Tests `token_encode` and `token_size` independently. Checks known patterns, size consistency, and error handling.

**What it proves:** Token encoding matches the format the decoder expects.

### Unit: Emitter

Tests the Emitter class directly: DEFINE_OP emission, idempotency of `ensure_defined`, measurement accuracy (measured size == actual emitted size), finalize output, TARGET_EXTENDED suppression.

**What it proves:** The measurement methods are accurate (critical for Pass 1 correctness), DEFINE_OP is emitted exactly once, TARGET_EXTENDED works correctly.

### Unit: Program Builder

Tests the Program class: instruction count, label placement, duplicate label detection, Slots allocation.

**What it proves:** The frontend API has correct semantics before any encoding.

### Integration: Encoder → Python Simulator

Full round-trip tests running through the Python simulator. Covers: basic SET/ADD, SUB/MUL/NEG via DEFINE_OP, counting loops, sum 1..10=55, factorial(7)=5040, fibonacci(10)=55, floats, DEFINE_OP behaviour, Elias efficiency, stream growth, disassembler.

**What it proves:** The encoder produces bitstreams that a conforming decoder will execute correctly. Tests complete programs, not just individual instructions.

### Integration: Encoder → C Decoder

The same programs run through `zce_decoder_v2.h` via the compiled C harness. Basic ops, control flow, floats. Plus a cross-check section that runs each program through both the Python simulator and the C decoder and asserts every non-zero slot matches.

**What it proves:** Wire format compatibility. The Python encoder and the C decoder agree on every bit of every instruction. This is the critical test — it proves the system actually works end-to-end across a language boundary.

### Unit: C Decoder Error Cases

Directly crafts malformed streams and verifies the C decoder handles them correctly: undefined token produces error, JMP out of bounds produces error, empty stream succeeds, JIF not taken with bad target does not error.

**What it proves:** The decoder's error handling is correct. Malformed streams don't crash. Forward-compatible behaviour (JIF not taken with unreachable bad target) works correctly.

---

## Running the Tests

```bash
# Compile the C harness first
gcc -O2 -o zce_harness zce_harness.c -lm

# Run all 297 tests
python3 zce_tests.py

# Run only Python tests (no C compiler needed)
python3 zce_tests.py --no-c

# Verbose output
python3 zce_tests.py -v
```

Expected output: `297/297 passed ✓ ALL PASS`

---

## Adding a New Op

When you want to add a new operation to the system, you need to touch exactly three files. Here is the complete procedure:

**Step 1: Assign an op code and token slot.**

Pick an op code (the integer `exec_ext` will receive) that doesn't conflict with existing ones. Pick a token slot (the N in the token format) that doesn't conflict. Op codes 3-9 are integer ops, 10-26 are float ops, 30+ are I/O. Token slots 6-63 are available.

**Step 2: Implement in `zce_stdlib.h`.**

Add a `#define ZCE_OP_YOURNAME N` and a `case ZCE_OP_YOURNAME:` in `zce_stdlib_exec`.

**Step 3: Register in `zce_encoder.py`.**

Add to `OP_YOURNAME = N` constants, `STDLIB_TOKEN['YOURNAME'] = slot`, `STDLIB_DEFS['YOURNAME'] = (n_args, arg_types, action, op_code)`, and a method on the `Program` class.

**Step 4: Update the Python simulator in `zce_tests.py`.**

Add the op to `EXTENDED_OPS` dict.

**Step 5: Write tests.**

Add unit/integration tests. Run the suite. All previous tests must still pass.

The decoder (`zce_decoder_v2.h`) is never touched. This is the correct workflow.

---

## What Comes Next

The encoder as it exists is complete for producing ZCE streams from Python. The logical next steps are:

**C encoder (`zce_encoder.c`)** — for generating streams on embedded hardware at runtime. The Python encoder already proves the algorithm. A C implementation follows the same two-pass structure.

**Assembler** — a text-based ZCE assembly language with a parser that produces Program objects. Makes ZCE programs writable by humans without Python.

**LLVM backend** — a ZCE target for LLVM so that C, Rust, and other languages can compile to ZCE streams. The encoder's two-pass structure maps cleanly to LLVM's code generation model.

**ML library** — a ZCE library for neural network inference. The float ops (FTANH, FEXP, FMUL, FADD, FSUB) are exactly what you need for forward passes through fully-connected and recurrent networks. `zce_ml.h` as an exec_ext alongside `zce_stdlib.h`.
