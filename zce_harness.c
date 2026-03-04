/*
 * zce_harness.c  —  ZCE test harness
 * Reads a ZCE stream from stdin, runs it, prints non-zero slot values.
 * Used by zce_tests.py for C integration tests.
 *
 * Compile: gcc -O2 -o zce_harness zce_harness.c -lm
 */
#include "zce_decoder_v2.h"
#include "zce_stdlib.h"

int main(void) {
    uint8_t buf[65536]; int len = 0, c;
    while ((c = fgetc(stdin)) != EOF && len < 65535)
        buf[len++] = (uint8_t)c;

    ZCEDecoder d;
    zce_init(&d, buf, len);
    d.exec_ext = zce_stdlib_exec;
    int ret = zce_run(&d);

    if (ret != 0) {
        fprintf(stdout, "ERROR: %s\n", d.errmsg);
        return 1;
    }

    int printed = 0;
    for (int i = 0; i < 4096 && printed < 64; i++) {
        if (d.slots[i] != 0) {
            uint32_t raw = (uint32_t)d.slots[i];
            uint32_t e8  = (raw >> 23) & 0xFF;
            if (e8 > 0 && e8 < 255) {
                float fv; memcpy(&fv, &raw, 4);
                printf("slot[%d] = %lld  (float: %.6f)\n",
                       i, (long long)d.slots[i], fv);
            } else {
                printf("slot[%d] = %lld\n", i, (long long)d.slots[i]);
            }
            printed++;
        }
    }
    if (!printed) printf("(all slots zero)\n");
    printf("steps=%d\n", d.steps);
    return 0;
}
