/*
 * zce_stdlib.h  —  ZCE Standard Extended Operations
 * ===================================================
 *
 * Provides the standard exec_ext implementation for zce_decoder_v2.h.
 * Covers integer arithmetic, float ops, and basic I/O.
 *
 * USAGE
 *   #include "zce_decoder_v2.h"
 *   #include "zce_stdlib.h"
 *
 *   ZCEDecoder d;
 *   zce_init(&d, data, len);
 *   d.exec_ext = zce_stdlib_exec;
 *   zce_run(&d);
 *
 * OP CODES  (must match zce_encoder.py exactly)
 *
 *   Integer:
 *     3  SUB    slots[dst] = slots[a] - slots[b]
 *     4  MUL    slots[dst] = slots[a] * slots[b]
 *     5  DIV    slots[dst] = slots[a] / slots[b]  (integer, div by zero → 0)
 *     6  NEG    slots[dst] = -slots[a]
 *     7  MOD    slots[dst] = slots[a] % slots[b]  (mod by zero → 0)
 *     8  AND    slots[dst] = slots[a] & slots[b]
 *     9  OR     slots[dst] = slots[a] | slots[b]
 *
 *   Float (IEEE 754 single precision, stored as raw bits in int64 slot):
 *    10  FSET   slots[dst] = raw32 float literal
 *    11  FADD   slots[dst] = f32(slots[a]) + f32(slots[b])
 *    12  FSUB   slots[dst] = f32(slots[a]) - f32(slots[b])
 *    13  FMUL   slots[dst] = f32(slots[a]) * f32(slots[b])
 *    14  FDIV   slots[dst] = f32(slots[a]) / f32(slots[b])  (div by zero → 0)
 *    15  FEXP   slots[dst] = expf(slots[a])   (clamped to 88.0 to avoid inf)
 *    16  FTANH  slots[dst] = tanhf(slots[a])
 *    17  FSQRT  slots[dst] = sqrtf(slots[a])
 *    18  FABS   slots[dst] = fabsf(slots[a])
 *    19  FMAX   slots[dst] = fmaxf(slots[a], slots[b])
 *    20  FMIN   slots[dst] = fminf(slots[a], slots[b])
 *    21  FNEG   slots[dst] = -f32(slots[a])
 *    22  FFLOOR slots[dst] = floorf(slots[a])
 *    23  FCEIL  slots[dst] = ceilf(slots[a])
 *    24  FINT   slots[dst] = (int64) f32(slots[a])  (float to int)
 *    25  FFROM  slots[dst] = f32 from int slots[a]  (int to float)
 *    26  FCMP   slots[dst] = f32(slots[a]) > f32(slots[b]) ? 1 : 0
 *
 *   I/O:
 *    30  PRINT  print slots[a] as decimal integer to stdout
 *    31  PRINTF print slots[a] as float to stdout
 *
 * FLOAT ENCODING
 *   Floats are stored as raw IEEE 754 bits in an int64 slot.
 *   The upper 32 bits are zero. Use FSET (with ARG_RAW32) to load literals.
 *   Use FINT/FFROM to convert between float and integer representations.
 *
 * This file is NOT frozen. New ops can be added as needed.
 * The decoder (zce_decoder_v2.h) never changes regardless of what is added here.
 */

#ifndef ZCE_STDLIB_H
#define ZCE_STDLIB_H

#include <math.h>
#include <stdio.h>
#include <stdint.h>
#include <string.h>

/* ── Op codes ────────────────────────────────────────────── */

#define ZCE_OP_SUB    3
#define ZCE_OP_MUL    4
#define ZCE_OP_DIV    5
#define ZCE_OP_NEG    6
#define ZCE_OP_MOD    7
#define ZCE_OP_AND    8
#define ZCE_OP_OR     9

#define ZCE_OP_FSET   10
#define ZCE_OP_FADD   11
#define ZCE_OP_FSUB   12
#define ZCE_OP_FMUL   13
#define ZCE_OP_FDIV   14
#define ZCE_OP_FEXP   15
#define ZCE_OP_FTANH  16
#define ZCE_OP_FSQRT  17
#define ZCE_OP_FABS   18
#define ZCE_OP_FMAX   19
#define ZCE_OP_FMIN   20
#define ZCE_OP_FNEG   21
#define ZCE_OP_FFLOOR 22
#define ZCE_OP_FCEIL  23
#define ZCE_OP_FINT   24
#define ZCE_OP_FFROM  25
#define ZCE_OP_FCMP   26

#define ZCE_OP_PRINT  30
#define ZCE_OP_PRINTF 31

/* ── Float bit-cast helpers ──────────────────────────────── */

static float zce_bits_to_f32(uint64_t raw) {
    float f;
    uint32_t u = (uint32_t)raw;
    memcpy(&f, &u, 4);
    return f;
}

static uint32_t zce_f32_to_bits(float f) {
    uint32_t u;
    memcpy(&u, &f, 4);
    return u;
}

/* Slot read/write as float */
#define ZCE_GETF(d, i)    zce_bits_to_f32((uint64_t)(d)->slots[(i)])
#define ZCE_PUTF(d, i, f) ((d)->slots[(i)] = (int64_t)zce_f32_to_bits(f))

/* ── Standard executor ───────────────────────────────────── */

static void zce_stdlib_exec(ZCEDecoder *d, int op, int64_t *a) {
    switch (op) {

        /* ── Integer arithmetic ── */
        case ZCE_OP_SUB:
            d->slots[a[0]] = d->slots[a[1]] - d->slots[a[2]];
            break;
        case ZCE_OP_MUL:
            d->slots[a[0]] = d->slots[a[1]] * d->slots[a[2]];
            break;
        case ZCE_OP_DIV:
            d->slots[a[0]] = d->slots[a[2]] != 0
                ? d->slots[a[1]] / d->slots[a[2]] : 0;
            break;
        case ZCE_OP_NEG:
            d->slots[a[0]] = -d->slots[a[1]];
            break;
        case ZCE_OP_MOD:
            d->slots[a[0]] = d->slots[a[2]] != 0
                ? d->slots[a[1]] % d->slots[a[2]] : 0;
            break;
        case ZCE_OP_AND:
            d->slots[a[0]] = d->slots[a[1]] & d->slots[a[2]];
            break;
        case ZCE_OP_OR:
            d->slots[a[0]] = d->slots[a[1]] | d->slots[a[2]];
            break;

        /* ── Float arithmetic ── */
        case ZCE_OP_FSET:
            /* a[1] is raw32 bits already decoded into int64 by ARG_RAW32 */
            d->slots[a[0]] = (int64_t)(uint32_t)a[1];
            break;
        case ZCE_OP_FADD:
            ZCE_PUTF(d, a[0], ZCE_GETF(d, a[1]) + ZCE_GETF(d, a[2]));
            break;
        case ZCE_OP_FSUB:
            ZCE_PUTF(d, a[0], ZCE_GETF(d, a[1]) - ZCE_GETF(d, a[2]));
            break;
        case ZCE_OP_FMUL:
            ZCE_PUTF(d, a[0], ZCE_GETF(d, a[1]) * ZCE_GETF(d, a[2]));
            break;
        case ZCE_OP_FDIV: {
            float b = ZCE_GETF(d, a[2]);
            ZCE_PUTF(d, a[0], b != 0.0f ? ZCE_GETF(d, a[1]) / b : 0.0f);
            break;
        }
        case ZCE_OP_FEXP:
            /* Clamp to 88.0 to avoid inf on small FPUs */
            ZCE_PUTF(d, a[0], expf(fminf(ZCE_GETF(d, a[1]), 88.0f)));
            break;
        case ZCE_OP_FTANH:
            ZCE_PUTF(d, a[0], tanhf(ZCE_GETF(d, a[1])));
            break;
        case ZCE_OP_FSQRT:
            ZCE_PUTF(d, a[0], sqrtf(ZCE_GETF(d, a[1])));
            break;
        case ZCE_OP_FABS:
            ZCE_PUTF(d, a[0], fabsf(ZCE_GETF(d, a[1])));
            break;
        case ZCE_OP_FMAX:
            ZCE_PUTF(d, a[0], fmaxf(ZCE_GETF(d, a[1]), ZCE_GETF(d, a[2])));
            break;
        case ZCE_OP_FMIN:
            ZCE_PUTF(d, a[0], fminf(ZCE_GETF(d, a[1]), ZCE_GETF(d, a[2])));
            break;
        case ZCE_OP_FNEG:
            ZCE_PUTF(d, a[0], -ZCE_GETF(d, a[1]));
            break;
        case ZCE_OP_FFLOOR:
            ZCE_PUTF(d, a[0], floorf(ZCE_GETF(d, a[1])));
            break;
        case ZCE_OP_FCEIL:
            ZCE_PUTF(d, a[0], ceilf(ZCE_GETF(d, a[1])));
            break;
        case ZCE_OP_FINT:
            /* Float → integer truncation */
            d->slots[a[0]] = (int64_t)ZCE_GETF(d, a[1]);
            break;
        case ZCE_OP_FFROM:
            /* Integer → float */
            ZCE_PUTF(d, a[0], (float)d->slots[a[1]]);
            break;
        case ZCE_OP_FCMP:
            /* 1 if a > b, 0 otherwise. Use with JIF for float comparisons. */
            d->slots[a[0]] = ZCE_GETF(d, a[1]) > ZCE_GETF(d, a[2]) ? 1 : 0;
            break;

        /* ── I/O ── */
        case ZCE_OP_PRINT:
            printf("%lld\n", (long long)d->slots[a[0]]);
            break;
        case ZCE_OP_PRINTF:
            printf("%f\n", (double)ZCE_GETF(d, a[0]));
            break;

        default:
            /* Unknown op — silently skip. Forward compatible. */
            break;
    }
}

#endif /* ZCE_STDLIB_H */
