/*
 * dirtyrand — negative control for the checkpoint-compression gate.
 *
 * Same store pattern as dirtyzero, but every word written is the next
 * state of a 32-bit Galois LFSR, so each dirty line holds eight unrelated
 * pseudo-random words: incompressible under every BDI sub-encoder.
 *
 * Expected: probe ratio ~1.0x (verbatim fallback), zero-block count ~0.
 *
 * This is the control that catches a broken probe. A hook reading a stale
 * or zeroed data pointer would sail through dirtyzero with a perfect
 * score and report every later benchmark as compressible; it cannot fake
 * ~1.0x here. Only the pair together validates the instrument.
 */

#include <stdint.h>
#include <stdio.h>

#define WORDS 8192            /* 32 kB: 2x the largest swept cache */
#define PASSES 2400           /* ~19.7M stores */

static volatile uint32_t buf[WORDS] __attribute__((aligned(64)));

int
main(void)
{
    uint32_t lfsr = 0xACE1u;

    for (unsigned p = 0; p < PASSES; p++)
        for (unsigned i = 0; i < WORDS; i++) {
            lfsr = (lfsr >> 1) ^ (-(lfsr & 1u) & 0xA3000000u);
            buf[i] = lfsr;
        }

    printf("dirtyrand done buf0=%u\n", buf[0]);
    return 0;
}
