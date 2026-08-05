/*
 * wstream — the weight-stream inference kernel: how much of an inference's
 * energy is actually spent fetching weights from NVM?
 *
 * This exists to measure one number that no published source supplies. The
 * TinyML argument is that storing model weights compressed in NVM cuts the
 * per-inference fetch energy, and that this lands in the one placement where
 * the energy identity is unconditionally favourable: a read-only stream, no
 * writeback, no dirty-set inflation, no capacity collateral. Whether that is
 * worth anything depends entirely on what fraction of inference energy the
 * weight fetch is -- and SONIC/TAILS (ASPLOS'19), the obvious second price
 * sheet, never reports it. Its published breakdown is all overhead terms
 * (26% control instructions, 14% FRAM writes to loop indices, ~40% fetch and
 * decode), with no figure for weight loading and no split of FRAM reads from
 * writes. So it gets measured here instead.
 *
 * THE RATIO IS THE WHOLE ANSWER, SO IT IS THE SWEEP AXIS
 * -----------------------------------------------------
 * A single benchmark could be rigged to any conclusion by choosing how much
 * arithmetic accompanies each fetched byte, so that choice is the parameter:
 *
 *     reuse = MACs performed per weight byte
 *
 * It is not a free knob -- it is set by the layer type, and real models span
 * its whole range:
 *
 *   reuse = 1        fully-connected at batch=1. Each weight multiplies
 *                    exactly one activation and is never seen again. The
 *                    autoencoder (anomaly detection) is all of this.
 *   reuse = H*W      convolution. One filter weight sweeps every output
 *                    spatial position, so a 1x1 conv on a 6x6 feature map
 *                    reuses each weight 36 times. MobileNet's late layers
 *                    live here.
 *   reuse = 100s     early convs on large feature maps.
 *
 * Weight traffic per inference is fixed at WEIGHT_BYTES; compute scales with
 * reuse. So sweeping reuse sweeps the compute:traffic ratio, and the answer
 * is a curve rather than one number that happens to flatter whichever side
 * the benchmark was written to favour.
 *
 * The weight is loaded ONCE into a local and then applied `reuse` times, which
 * is what a real kernel does (weight held in a register, swept across output
 * positions). Loading it inside the reuse loop instead would inflate the
 * instruction count by a factor of reuse without changing NVM traffic at all,
 * which would understate the fetch share -- a conservative error, but an error.
 *
 * WHAT THIS MEASURES AND WHAT IT DOES NOT
 * ---------------------------------------
 * Measured: the energy decomposition of an inference on this machine, with
 * NVM read energy priced per byte by the same calibrated knob every other
 * result in this campaign uses.
 *
 * NOT measured: compressed weight storage itself. No compressed-NVM mechanism
 * is simulated. The saving is a projection -- a codec of ratio R cuts the
 * weight stream's bytes by R, so it saves nvmReadEnergy * (1 - 1/R) -- and
 * the report labels it as such. That projection is only worth computing if
 * the measured fetch share is large, which is the question.
 *
 * Sizing: 64 kB of weights is MLPerf-Tiny scale (kws_dscnn 18.5 kB,
 * ic_resnet8 74 kB, vww_mobilenet 200 kB). The activation set is 64 B so it
 * coexists with a streaming weight line inside the 256 B L1 of Table I
 * without thrashing it -- at 32 B lines that is 2 lines of activations and 1
 * of weights against 8 sets.
 *
 * The weight array is volatile so the stream cannot be optimised away, and it
 * lives in BSS: its contents are irrelevant here because uncompressed NVM
 * reads are priced per byte regardless of what the bytes are. Content only
 * starts to matter once a compressed-storage mechanism is actually built.
 *
 * Usage: wstream [reuse] [inferences]
 * Runtime scales with reuse * inferences, so high-reuse rows should use fewer
 * inferences; the report normalises per inference and reports fractions, so
 * rows with different inference counts remain comparable.
 */

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#define WEIGHT_BYTES 65536    /* 64 kB model: MLPerf-Tiny scale */
#define ACT_WORDS 16          /* 64 B resident activations, power of two */

static volatile int8_t weights[WEIGHT_BYTES] __attribute__((aligned(64)));
static int32_t act[ACT_WORDS] __attribute__((aligned(64)));

int
main(int argc, char **argv)
{
    unsigned reuse = (argc > 1) ? (unsigned)atoi(argv[1]) : 1;
    unsigned inferences = (argc > 2) ? (unsigned)atoi(argv[2]) : 4;
    int32_t acc = 0;

    if (reuse == 0)
        reuse = 1;

    for (unsigned i = 0; i < ACT_WORDS; i++)
        act[i] = (int32_t)(i + 1);

    for (unsigned n = 0; n < inferences; n++) {
        for (unsigned i = 0; i < WEIGHT_BYTES; i++) {
            /* one fetch per weight byte -- the NVM traffic of an inference */
            int32_t w = weights[i];

            /* ...applied `reuse` times, as a conv sweeps output positions */
            for (unsigned r = 0; r < reuse; r++)
                acc += w * act[r & (ACT_WORDS - 1)];
        }
    }

    printf("wstream reuse=%u inferences=%u acc=%d\n", reuse, inferences, acc);
    return 0;
}
