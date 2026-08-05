/*
 * compfit — capacity control for the compressed cache.
 *
 * Repeatedly scans a BDI-trivial (all-zero) working set sized to sit
 * between the uncompressed and compressed capacity of the 256 B, 2-way,
 * 32 B-line L1D used across the sweeps:
 *
 *   - Uncompressed (BaseSetAssoc): 4 sets x 2 ways = 8 lines (256 B).
 *     The 12-line array maps 3 lines to every set, so a circular scan
 *     under LRU misses on every access.
 *   - Compressed (CompressedTags, max ratio 2): tags cover 2x the size,
 *     i.e. 4 sets x 2 superblock ways, each way co-allocating both 32 B
 *     sub-blocks when they compress to half a line or less. Zero lines
 *     compress far below that, so all 6 superblocks (12 lines) fit and
 *     the scan hits in steady state.
 *
 * A compressed cache should therefore beat the uncompressed one by
 * roughly the ReRAM read latency per access. MiBench kernels cannot show
 * this effect because their fills almost never compress (I-cache ~100%
 * incompressible, D-cache 88-100%); this benchmark separates "the data
 * does not compress" from "the cache cannot exploit compression".
 *
 * The array is superblock-aligned so it covers exactly 12 lines, and
 * volatile so every element read reaches the cache.
 */

#include <stdint.h>
#include <stdio.h>

#define WORDS 96          /* 384 B = 12 lines = 6 superblocks */
#define PASSES 200000     /* ~19M loads, comparable to sha's memOp count */

static volatile uint32_t arr[WORDS] __attribute__((aligned(64)));

int
main(void)
{
    uint32_t sum = 0;

    for (unsigned i = 0; i < WORDS; i++)
        arr[i] = 0;

    for (unsigned p = 0; p < PASSES; p++)
        for (unsigned i = 0; i < WORDS; i++)
            sum += arr[i];

    printf("sum=%u\n", sum);
    return 0;
}
