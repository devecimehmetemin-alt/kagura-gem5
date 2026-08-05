/*
 * dirtyzero — positive control for the checkpoint-compression gate.
 *
 * The gate (--ckpt-gate) probes dirty blocks at each checkpoint with a
 * measurement-only BDI. A gate result on real benchmarks is only
 * trustworthy if the probe demonstrably reads both extremes, so this
 * writes the most compressible dirty set possible: a steady stream of
 * zero stores over a buffer 2x the largest swept cache (32 kB vs 16 kB),
 * keeping the cache full of dirty all-zero lines at every checkpoint.
 *
 * Expected: probe ratio near BDI's zero-line ceiling, and
 * ckptProbeZeroBlocks ~= ckptProbeBlocks. Anything else means the probe
 * is broken, not the data.
 *
 * Its twin dirtyrand writes LFSR noise and must read ~1.0x. The pair
 * brackets the probe: dirtyrand is the one that catches a hook reading
 * stale zeroed memory, which would pass this control and lie on
 * everything after it.
 *
 * ~19.7M stores (2400 passes x 8192 words), comparable to compfit's
 * load count, so checkpoint counts land in the familiar range.
 */

#include <stdint.h>
#include <stdio.h>

#define WORDS 8192            /* 32 kB: 2x the largest swept cache */
#define PASSES 2400           /* ~19.7M stores */

static volatile uint32_t buf[WORDS] __attribute__((aligned(64)));

int
main(void)
{
    for (unsigned p = 0; p < PASSES; p++)
        for (unsigned i = 0; i < WORDS; i++)
            buf[i] = 0;

    printf("dirtyzero done buf0=%u\n", buf[0]);
    return 0;
}
