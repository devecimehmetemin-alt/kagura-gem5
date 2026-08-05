/*
 * mixfit_stream - compfit_stream with alternating compressibility.
 * Streamed lines alternate between an all-zero buffer (BDI compresses the
 * whole line) and an LCG-filled buffer (BDI stores it raw). Resident set,
 * pass structure, stream rate and load count are unchanged, so the only
 * variable between phases is the byte content of the fills.
 * PHASE_PASSES sets phase length against power-cycle length: a run is
 * 200000 passes across ~30 power cycles, so 6250 is one phase per cycle.
 */
#include <stdint.h>
#include <stdio.h>

#define WORDS 96
#define STREAM_WORDS 16384
#define LINE_WORDS 8
#define STREAM_PER_PASS 2
#define PASSES 200000

#ifndef PHASE_PASSES
#define PHASE_PASSES 6250
#endif

static volatile uint32_t arr[WORDS] __attribute__((aligned(64)));
static volatile uint32_t stream_z[STREAM_WORDS] __attribute__((aligned(64)));
static volatile uint32_t stream_r[STREAM_WORDS] __attribute__((aligned(64)));

int
main(void)
{
    uint32_t sum = 0, r = 0x12345678u;
    unsigned s = 0, left = PHASE_PASSES, phase = 0;
    volatile uint32_t *stream = stream_z;

    for (unsigned i = 0; i < WORDS; i++)
        arr[i] = 0;
    for (unsigned i = 0; i < STREAM_WORDS; i++) {
        r = r * 1664525u + 1013904223u;
        stream_r[i] = r;
    }

    for (unsigned p = 0; p < PASSES; p++) {
        if (left-- == 0) {
            phase ^= 1u;
            stream = phase ? stream_r : stream_z;
            left = PHASE_PASSES;
            s = 0;
        }
        for (unsigned i = 0; i < WORDS; i++)
            sum += arr[i];
        for (unsigned k = 0; k < STREAM_PER_PASS; k++) {
            sum += stream[s];
            s += LINE_WORDS;
            if (s >= STREAM_WORDS)
                s = 0;
        }
    }
    printf("sum=%u\n", sum);
    return 0;
}
