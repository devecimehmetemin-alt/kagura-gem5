/*
 * compfit_stream — the co-occurrence test: capacity win + late-cycle fills.
 *
 * compfit proved the capacity channel (a resident all-zero set that only
 * fits compressed) but left Kagura nothing to save: its fills are
 * front-loaded into the post-reboot refill, so the Regular Mode tail at
 * the end of a power cycle contains no compression activity at all
 * (measured: Kagura's compression-energy saving on compfit is zero).
 * The workloads where compression pays and the window where Kagura acts
 * never overlapped.
 *
 * This benchmark makes them overlap, the way a streaming media kernel
 * would: the resident set of compfit (12 zero lines against 8
 * uncompressed / 16 compressed slots) supplies the capacity win for ACC,
 * and a large all-zero buffer walked one line at a time supplies a steady
 * trickle of compressible, never-reused fills — compression work spread
 * across the whole power cycle, including the tail. A correct Kagura
 * should drop exactly the tail's share of that work at no capacity cost
 * (a streamed line is dead on arrival), landing between ACC and the
 * uncompressed baseline: the kagura > acc > none ordering that neither
 * compfit (no late fills) nor MiBench (no compressible data) can show.
 *
 * The stream rate is a balance: each streamed line evicts one resident
 * or stream line from its set, so too high a rate thrashes away the
 * capacity win the test needs ACC to keep. Two lines per 96-load resident
 * pass (~2% of loads) keeps ACC's hit rate near compfit's while still
 * putting ~4 compression operations in every pass.
 *
 * The stream buffer lives in BSS (all zeros, BDI-trivial) and is longer
 * than any plausible run segment between reboots, so a wrapped-around
 * line has long been evicted and misses again — every stream touch is a
 * fill.
 */

#include <stdint.h>
#include <stdio.h>

#define WORDS 96              /* resident: 384 B = 12 lines = 6 superblocks */
#define STREAM_WORDS 16384    /* stream: 64 kB = 2048 lines */
#define LINE_WORDS 8          /* 32 B lines */
#define STREAM_PER_PASS 2     /* stream lines touched per resident pass */
#define PASSES 200000         /* ~19.6M loads, comparable to compfit */

static volatile uint32_t arr[WORDS] __attribute__((aligned(64)));
static volatile uint32_t stream[STREAM_WORDS] __attribute__((aligned(64)));

int
main(void)
{
    uint32_t sum = 0;
    unsigned s = 0;

    for (unsigned i = 0; i < WORDS; i++)
        arr[i] = 0;

    for (unsigned p = 0; p < PASSES; p++) {
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
