#include "ehs/kagura_controller.hh"

#include <algorithm>

#include "base/logging.hh"
#include "base/trace.hh"
#include "cpu/base.hh"
#include "debug/Kagura.hh"
#include "ehs/energy_compressor.hh"

namespace gem5
{

KaguraController::KaguraController(const Params &params)
  : IntermittentController(params),
    rMem(0),
    rPrev(0),
    rThres(params.n_thres_init),
    rEvict(0),
    rAdjust(0),
    satCounter(params.sat_counter_init),
    regularMode(false),
    haveHistory(false),
    closenessFrac(params.closeness_frac),
    aimdEnabled(params.aimd),
    thresCapFrac(params.thres_cap_frac),
    rmConfidenceMin(params.rm_confidence_min),
    gateBlocked(false),
    percEnabled(params.rm_perceptron),
    percHistLen(params.perceptron_history),
    percTheta(params.perceptron_theta > 0 ? params.perceptron_theta
              : int(1.93 * params.perceptron_history + 14)),
    percVolatile(params.perceptron_volatile),
    percWeights(params.perceptron_history + 1, 0),
    percHistory(params.perceptron_history, 0),
    percY(0),
    cycleIdx(0),
    kaguraStats(this)
{
    fatal_if(rThres == 0, "n_thres_init must be nonzero: a zero threshold "
             "can never be reached and Regular Mode would never trigger");
    fatal_if(satCounter > 3, "sat_counter_init is a 2-bit value (0..3)");
    fatal_if(rmConfidenceMin > 3,
             "rm_confidence_min compares against a 2-bit counter (0..3); "
             "above 3 the gate can never open and Regular Mode is "
             "unreachable, which --compression acc already expresses");
    fatal_if(thresCapFrac < 0.0 || thresCapFrac >= 1.0,
             "thres_cap_frac must be in [0, 1): at 1 or above the cap "
             "admits a threshold spanning the whole estimated cycle, which "
             "is the failure mode it exists to rule out");
    fatal_if(thresCapFrac > 0.0 && !aimdEnabled,
             "thres_cap_frac bounds the AIMD update; with aimd disabled "
             "there is nothing to bound");
    fatal_if(percEnabled && (percHistLen < 1 || percHistLen > 64),
             "perceptron_history must be 1..64");

    if (!params.decision_dump_path.empty()) {
        decisionDump.open(params.decision_dump_path);
        decisionDump <<
            "# per power cycle, state as the cycle ended and before the\n"
            "# reboot update: the RTL replays R_mem increments against\n"
            "# R_prev and R_thres, then applies the same AIMD step.\n"
            "# cycle R_prev R_thres R_mem R_adjust R_evict sat y rm gate\n";
    }
}

void
KaguraController::regProbeListeners()
{
    // The registers sit conceptually at the LSQ. the closest
    // observation point gem5 offers is the commit probes, which MinorCPU
    // fires once per committed load/store via BaseCPU::probeInstCommit.
    typedef ProbeListenerArg<KaguraController, uint64_t> Listener;
    listeners.push_back(cpu->getProbeManager()->connect<Listener>(
        this, "RetiredLoads", &KaguraController::memOpCommitted));
    listeners.push_back(cpu->getProbeManager()->connect<Listener>(
        this, "RetiredStores", &KaguraController::memOpCommitted));
}

void
KaguraController::broadcastMode(bool regular)
{
    for (auto *compressor : compressors)
        compressor->setRegularMode(regular);
}

void
KaguraController::memOpCommitted(const uint64_t &count)
{
    rMem += count;
    kaguraStats.memOps += count;

    if (regularMode) {
        kaguraStats.memOpsRegularMode += count;
        return;
    }

    // No previous cycle yet
    if (!haveHistory)
        return;

    // Once the gate has vetoed a trigger this cycle there is nothing left
    // to evaluate. satCounter only moves at power-off, so the verdict
    // cannot change until the next cycle.
    if (gateBlocked)
        return;

    // N_remain = N_prev - N_mem. signed, because the current cycle
    // can outrun the estimate.
    const int64_t nRemain = int64_t(rPrev) - int64_t(rMem);
    if (nRemain <= int64_t(rThres)) {
        // Confidence gate: enter Regular Mode only if the estimator's own
        // scorekeeping says the one cycle history has been estimating well.
        // so on erratic harvests the veto keeps Kagura at plain-ACC behavior
        // instead of letting a broken estimate uncompress the rest of the
        // cycle.
        const bool percVeto = percEnabled && percY < 0;
        const bool satVeto = rmConfidenceMin > 0 &&
                             satCounter < rmConfidenceMin;
        const bool veto = percVeto || satVeto;
        if (veto) {
            gateBlocked = true;
            if (percVeto) {
                DPRINTF(Kagura,
                        "GATE VETO       N_remain=%d <= N_thres=%d but "
                        "y=%d < 0: staying in Compression Mode\n",
                        nRemain, rThres, percY);
            } else {
                DPRINTF(Kagura,
                        "GATE VETO       N_remain=%d <= N_thres=%d but "
                        "cnt=%d < %d: staying in Compression Mode\n",
                        nRemain, rThres, satCounter, rmConfidenceMin);
            }
            return;
        }
        regularMode = true;
        kaguraStats.decisionPoints++;
        broadcastMode(true);
        DPRINTF(Kagura,
                "DECISION POINT  N_remain=%d <= N_thres=%d "
                "(R_prev=%d R_mem=%d): Regular Mode\n",
                nRemain, rThres, rPrev, rMem);
    }
}

void
KaguraController::blockEvicted()
{
    // Blocks evicted since the decision point
    if (regularMode) {
        rEvict++;
        kaguraStats.rmEvictions++;
    }
}

void
KaguraController::powerOff()
{
    if (!haveHistory) {
        // First cycle
        rAdjust = 0;
        haveHistory = true;
    } else {
        // R_adjust = R_mem - R_prev
        rAdjust = int64_t(rMem) - int64_t(rPrev);

        // Reward/punishment: the 2-bit counter tracks whether history
        // has been estimating well.
        const uint64_t err = rAdjust < 0 ? -rAdjust : rAdjust;
        const bool close = double(err) <= closenessFrac * double(rPrev);
        if (close) {
            satCounter = std::min(satCounter + 1, 3u);
            kaguraStats.estimateRewards++;
        } else {
            satCounter = satCounter > 0 ? satCounter - 1 : 0;
            kaguraStats.estimatePunishments++;
        }

        // Perceptron training
        if (percEnabled) {
            const int t = close ? 1 : -1;
            if ((percY >= 0) != close)
                kaguraStats.percMispredicts++;
            const int mag = percY < 0 ? -percY : percY;
            if ((percY >= 0) != close || mag <= percTheta) {
                auto nudge = [](int w, int d) {
                    return std::max(-127, std::min(127, w + d));
                };
                percWeights[0] = nudge(percWeights[0], t);
                for (unsigned i = 0; i < percHistLen; i++)
                    percWeights[i + 1] =
                        nudge(percWeights[i + 1], t * percHistory[i]);
                kaguraStats.percUpdates++;
            }
            for (unsigned i = percHistLen - 1; i > 0; i--)
                percHistory[i] = percHistory[i - 1];
            percHistory[0] = t;
        }
    }

    if (!regularMode)
        kaguraStats.cyclesNoDecision++;
    if (gateBlocked)
        kaguraStats.cyclesGateSuppressed++;

    DPRINTF(Kagura,
            "CHECKPOINT      R_mem=%d R_prev=%d R_adjust=%d R_thres=%d "
            "R_evict=%d cnt=%d mode=%s\n",
            rMem, rPrev, rAdjust, rThres, rEvict, satCounter,
            regularMode ? "RM" : "CM");

    // Written here rather than at thawCpu because this is the last point
    // where R_thres and R_evict still hold the values the cycle actually
    // decided on: thawCpu overwrites both with the AIMD update.
    if (decisionDump.is_open()) {
        decisionDump << cycleIdx << ' ' << rPrev << ' ' << rThres << ' '
                     << rMem << ' ' << rAdjust << ' ' << rEvict << ' '
                     << satCounter << ' ' << percY << ' '
                     << (regularMode ? 1 : 0) << ' '
                     << (gateBlocked ? 1 : 0) << '\n';
        // gem5 exits without destructing SimObjects, so an
        // unflushed tail never reaches disk: at ~38 B a cycle the
        // last 8 KB buffer's worth of power cycles would vanish.
        decisionDump.flush();
    }
    cycleIdx++;

    IntermittentController::powerOff();
}

void
KaguraController::thawCpu()
{
    IntermittentController::thawCpu();

    // Recovery sequence. R_prev is rebuilt from the
    // restored R_mem rather than checkpointed
    rPrev = rMem;
    rMem = 0;

    // Apply the learned correction only while the confidence counter says
    // raw history has been off (00 or 01). clamped to 0
    if (satCounter <= 1) {
        const int64_t adjusted = int64_t(rPrev) + rAdjust;
        rPrev = adjusted > 0 ? uint64_t(adjusted) : 0;
        kaguraStats.adjustsApplied++;
    }

    // AIMD threshold tuning. Integer detail the paper's own
    // the 10% additive step is at least 1 (it moves 4 -> 5),
    // and halving floors at 1 so the decision point stays reachable.
    if (aimdEnabled) {
        if (gateBlocked) {
            kaguraStats.thresholdFrozen++;
        } else {
            if (rEvict > rThres / 2) {
                rThres = std::max<uint64_t>(rThres / 2, 1);
                kaguraStats.thresholdHalvings++;
            } else {
                rThres += std::max<uint64_t>(rThres / 10, 1);
                kaguraStats.thresholdIncreases++;
            }

            //clamp rThresh
            if (thresCapFrac > 0.0) {
                const uint64_t cap = std::max<uint64_t>(
                    uint64_t(thresCapFrac * double(rPrev)), 1);
                if (rThres > cap) {
                    rThres = cap;
                    kaguraStats.thresholdClamps++;
                }
            }
        }
    }
    rEvict = 0;
    gateBlocked = false;

    // The perceptron's result for the coming cycle,
    // computed here because its inputs only change at cycle boundaries.
    if (percEnabled) {
        if (percVolatile) {
            std::fill(percWeights.begin(), percWeights.end(), 0);
            std::fill(percHistory.begin(), percHistory.end(), 0);
        }
        percY = percWeights[0];
        for (unsigned i = 0; i < percHistLen; i++)
            percY += percWeights[i + 1] * percHistory[i];
    }

    if (regularMode) {
        regularMode = false;
        broadcastMode(false);
    }

    DPRINTF(Kagura,
            "REBOOT          R_prev=%d R_adjust=%d R_thres=%d cnt=%d y=%d\n",
            rPrev, rAdjust, rThres, satCounter, percY);
}


//stats object
KaguraController::KaguraStats::KaguraStats(statistics::Group *parent)
  : statistics::Group(parent),
    ADD_STAT(decisionPoints, statistics::units::Count::get(),
             "Decision points reached (compression disabled for the rest "
             "of the power cycle)"),
    ADD_STAT(cyclesNoDecision, statistics::units::Count::get(),
             "Power cycles that ended without reaching a decision point"),
    ADD_STAT(memOps, statistics::units::Count::get(),
             "Committed memory operations counted (loads + stores)"),
    ADD_STAT(memOpsRegularMode, statistics::units::Count::get(),
             "Of those, committed after the decision point (the Regular "
             "Mode tail)"),
    ADD_STAT(rmEvictions, statistics::units::Count::get(),
             "Blocks evicted during Regular Mode (what AIMD tunes on)"),
    ADD_STAT(estimateRewards, statistics::units::Count::get(),
             "Cycle estimates judged close (2-bit counter incremented)"),
    ADD_STAT(estimatePunishments, statistics::units::Count::get(),
             "Cycle estimates judged off (2-bit counter decremented)"),
    ADD_STAT(adjustsApplied, statistics::units::Count::get(),
             "Reboots where R_adjust was applied to R_prev"),
    ADD_STAT(thresholdIncreases, statistics::units::Count::get(),
             "AIMD additive threshold increases (low eviction pressure)"),
    ADD_STAT(thresholdHalvings, statistics::units::Count::get(),
             "AIMD multiplicative threshold halvings (high pressure)"),
    ADD_STAT(thresholdClamps, statistics::units::Count::get(),
             "Reboots where R_thres hit the R_prev-fraction cap "
             "(thres_cap_frac)"),
    ADD_STAT(thresholdFrozen, statistics::units::Count::get(),
             "AIMD reboots skipped because the gate vetoed the cycle, so "
             "R_evict measured no eviction pressure"),
    ADD_STAT(cyclesGateSuppressed, statistics::units::Count::get(),
             "Cycles where the confidence gate vetoed a decision point "
             "(rm_confidence_min or rm_perceptron)"),
    ADD_STAT(percMispredicts, statistics::units::Count::get(),
             "Perceptron: cycles whose close/off prediction was wrong"),
    ADD_STAT(percUpdates, statistics::units::Count::get(),
             "Perceptron: training updates (mispredict or |y| <= theta)")
{
}

} // namespace gem5
