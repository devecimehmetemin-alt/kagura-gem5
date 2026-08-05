#ifndef __EHS_KAGURA_CONTROLLER_HH__
#define __EHS_KAGURA_CONTROLLER_HH__

#include <cstdint>
#include <vector>

#include "base/statistics.hh"
#include "ehs/intermittent_controller.hh"
#include "params/KaguraController.hh"
#include "sim/probe/probe.hh"

namespace gem5
{

/**
 * Kagura: intermittence-aware mode switching for cache compression
 *
 * ACC's predictor asks "is compression making any gain?"; Kagura
 * asks the question ACC cannot: "will this block live long enough to earn
 * anything at all?" On an energy-harvesting system every compressed block
 * dies with the SRAM at the next power failure, so compression performed
 * near the end of a power cycle is pure loss no matter how compressible the
 * data is. Kagura estimates the number of memory operations remaining in
 * the current power cycle and, when it drops to a threshold, switches every
 * compressor to Regular Mode (compression off) for the rest of the cycle.
 *
 * The estimate is history-based: N_remain = R_prev - R_mem , where R_mem
 * counts memory operations committed so far in this
 * cycle and R_prev holds the (corrected) count of the previous cycle. The
 * correction is the improved approach: R_adjust = R_mem - R_prev is
 * computed at each power failure (Eq. 6) and applied to the next cycle's
 * R_prev -- but only while a 2-bit saturating counter, rewarded when the
 * estimate lands close to the actual count and punished otherwise, says the
 * raw history has been unreliable (counter <= 1).
 *
 * The threshold N_thres is tuned by AIMD on eviction pressure :
 * R_evict counts blocks evicted after the decision point; many
 * evictions mean the uncompressed cache was too small, so the threshold
 * halves, few  evictions mean there was room, so it grows 10%
 *
 * Implementation mapping:
 *
 *  - Committed memory operations is counted from the CPU's RetiredLoads /
 *    RetiredStores probes, which BaseCPU::probeInstCommit fires at commit.
 *    the same LSQ-commit point where the paper's registers sit (Fig. 7).
 *    The decision check (Eq. 5 against R_thres) runs in the probe handler,
 *    i.e. on every committed memory op, exactly as in the paper.
 *
 *  - The mode switch reaches the compressors through the inherited
 *    compressors vector: EnergyCompressor::setRegularMode() forces
 *    uncompressed fills, overriding ACC's own predictor.
 *
 *  - R_evict is fed by the D-cache (an ACCCache with its kagura parameter
 *    set), matching the paper's D-cache-centric architecture.
 *    Counting is internally gated to Regular Mode, "since the decision
 *    point".
 *
 *  - The registers survive power failures for free since SimObject members
 *    persist across the off period, so, like ACC's GCP, they are priced in
 *    the config's ckpt_reg_bytes instead of saved explicitly: R_mem,
 *    R_adjust, R_thres, R_evict (4 x 4 B) plus the 2-bit counter -- 17 B.
 *    R_prev is deliberately not checkpointed (Fig. 10): it is rebuilt on
 *    reboot from the restored R_mem.
 */

class KaguraController : public IntermittentController
{
  protected:
    /** Committed memory operations so far in this power cycle (R_mem). */
    uint64_t rMem;
    /** Estimated memory operations in a full cycle (R_prev, corrected). */
    uint64_t rPrev;
    /** Compression-disabling threshold (R_thres / N_thres). */
    uint64_t rThres;
    /** Blocks evicted since the decision point (R_evict). */
    uint64_t rEvict;
    /** Last cycle's estimation error, R_mem - R_prev (R_adjust, Eq. 6). */
    int64_t rAdjust;
    /** 2-bit saturating estimate-confidence counter (0..3). */
    unsigned satCounter;

    /** True from the decision point to the end of the power cycle. */
    bool regularMode;

    /**
     * False until the first power failure: cycle 1 has no previous cycle,
     * so R_prev is meaningless and the decision check must not run. Without
     * this, N_remain = 0 - R_mem would go negative on the first memory op
     * and the whole first cycle would run uncompressed.)
     */
    bool haveHistory;

    /** Reward the estimate when |R_adjust| <= closenessFrac * R_prev. */
    const double closenessFrac;

    /**
     * always true for paper design.
     */
    const bool aimdEnabled;

    /**
     * Bound R_thres to this fraction of R_prev at each reboot
     * Acts as a maximum cap
     */
    const double thresCapFrac;

    /**
     * minimum value for the vetoing mechanism
     */
    const unsigned rmConfidenceMin;

    /**
     * True once the decision condition has been met in this cycle while the
     * confidence gate held it back
     */
    bool gateBlocked;

    /**
     * Perceptron confidence gate: an alternative to the rm_confidence_min
     * veto,
     * a perceptron over the last perceptron_history cycle outcomes
     * the same close/off signal that trains the 2-bit counter. The
     * counter itself keeps running and keeps gating R_adjust,
     * so the gate mechanism is the only variable between the -c2
     * and -perc configurations.
     *
     * The two gates are NOT mutually exclusive. Enabling both makes the vetoes
     * a union: either alone suppresses the switch,
     * they fail on different harvest shapes so combination may perform better
     */
    const bool percEnabled;
    /** Outcome history length H (and weight count minus the bias). */
    const unsigned percHistLen;
    /** Training threshold theta. train on mispredict or |y| <= theta. */
    const int percTheta;
    /** Zero weights and history at each reboot */
    const bool percVolatile;

    /** Weights, percWeights[0] = bias. saturating at +/-127 (1 B each). */
    std::vector<int> percWeights;
    /** Cycle outcomes, most recent first: +1 close, -1 off, 0 unfilled. */
    std::vector<int> percHistory;
    /**
     * Dot product for the current cycle, computed once at reboot (history
     * and weights only change at cycle boundaries): y >= 0 predicts the
     * estimate will land close, so Regular Mode entry is allowed.
     */
    int percY;

    std::vector<ProbeListenerPtr<>> listeners;


    /** Bunch of statistics kept here, see below for what each is */
    struct KaguraStats : public statistics::Group
    {
        KaguraStats(statistics::Group *parent);

        /** Decision points reached (Compression -> Regular switches). */
        statistics::Scalar decisionPoints;
        /** Power cycles that ended without reaching a decision point. */
        statistics::Scalar cyclesNoDecision;
        /** Committed memory operations counted (loads + stores). */
        statistics::Scalar memOps;
        /** Of those, committed after the decision point (the RM tail). */
        statistics::Scalar memOpsRegularMode;
        /** Blocks evicted during Regular Mode (sum of R_evict). */
        statistics::Scalar rmEvictions;
        /** Cycle estimates judged close (2-bit counter incremented). */
        statistics::Scalar estimateRewards;
        /** Cycle estimates judged off (2-bit counter decremented). */
        statistics::Scalar estimatePunishments;
        /** Reboots where R_adjust was applied to R_prev (counter <= 1). */
        statistics::Scalar adjustsApplied;
        /** AIMD: additive threshold increases (low eviction pressure). */
        statistics::Scalar thresholdIncreases;
        /** AIMD: multiplicative threshold halvings (high pressure). */
        statistics::Scalar thresholdHalvings;
        /** AIMD: reboots where R_thres hit the R_prev-fraction cap. */
        statistics::Scalar thresholdClamps;
        /**
         * AIMD: reboots skipped because the gate vetoed the cycle, so R_evict
         * measured nothing.
         */
        statistics::Scalar thresholdFrozen;
        /** Cycles where the confidence gate vetoed a decision point. */
        statistics::Scalar cyclesGateSuppressed;
        /** Perceptron: cycles whose close/off prediction was wrong. */
        statistics::Scalar percMispredicts;
        /** Perceptron: training updates (mispredict or |y| <= theta). */
        statistics::Scalar percUpdates;
    } kaguraStats;

    /** Enter/leave Regular Mode on every compressor at once. */
    void broadcastMode(bool regular);

    /**
     * Probe handler, one call per committed load or store: the paper's
     * three-step sequence (R_mem += 1; N_remain = R_prev - R_mem; compare
     * against R_thres).
     */
    void memOpCommitted(const uint64_t &count);

    /** Power-on and do recovery sequence */
    void thawCpu() override;

  public:
    typedef KaguraControllerParams Params;
    KaguraController(const Params &params);

    void regProbeListeners() override;

    /** Power-cycle done, then the checkpoint. */
    void powerOff() override;

    /** Called by the D-cache (ACCCache) for every block it evicts. */
    void blockEvicted();
};

} // namespace gem5

#endif //__EHS_KAGURA_CONTROLLER_HH__
