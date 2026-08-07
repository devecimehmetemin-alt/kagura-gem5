#!/usr/bin/env bash
#
# Stage 6 sweep driver. Runs configs/kagura/stage_six_sweep.py over one axis at
# a time and collects the stats that matter into a single CSV, so the runs stay
# reproducible and the plots have one source.
#
# Run from the gem5 root:
#
#   bash configs/kagura/sweep.sh regress   # reproduce stage 5; check the auto-calibration
#   bash configs/kagura/sweep.sh size      # cache size x policy
#   bash configs/kagura/sweep.sh thres     # pinned N_thres (AIMD off)
#   bash configs/kagura/sweep.sh attrib    # is the saving Kagura's, or ACC's blind spot?
#   bash configs/kagura/sweep.sh wstream   # what share of inference energy is weight fetch?
#   bash configs/kagura/sweep.sh all
#
# Run 'regress' first: it is the gate on everything else.
#
# Each sweep appends to sweep/results.csv and leaves its full gem5 output under
# sweep/<label>/. Override the workload with e.g.
#   BIN=workloads/bin/bitcount BENCH_CWD=workloads/mibench/automotive/bitcount \
#   BENCH_OPTS=75000 bash configs/kagura/sweep.sh size
#
set -euo pipefail

GEM5=${GEM5:-./build/ARM/gem5.opt}
CFG=${CFG:-configs/kagura/stage_six_sweep.py}

BIN=${BIN:-workloads/bin/qsort_small}
BENCH_CWD=${BENCH_CWD:-workloads/mibench/automotive/qsort}
BENCH_OPTS=${BENCH_OPTS:-input_small.dat}

OUTROOT=${OUTROOT:-sweep}
CSV=$OUTROOT/results.csv

mkdir -p "$OUTROOT"

# Pull one stat out of a gem5 stats.txt. Missing stats (a policy that does not
# have them -- e.g. decisionPoints under --compression acc) come back as empty
# cells rather than killing the sweep.
stat_of () {
    awk -v key="$1" '$1 == key { print $2; exit }' "$2"
}

if [ ! -f "$CSV" ]; then
    echo "sweep,label,compression,l1_size,assoc,cacheline,capacitance,n_thres,aimd,icache_comp,nvmToCap,dirtyAware,thresCap,rmConf,eNvmRead,eNvmWrite,ePerInst,pStatic,\
simTicks,failures,compEnergy,ckptEnergy,ckptBytes,ckptDirtyBytes,\
nvmEnergy,nvmReadEnergy,nvmWriteEnergy,nvmReadBytes,nvmWriteBytes,\
dMissRate,iMissRate,dRewardTicks,iRewardTicks,\
memOps,memOpsRM,decisionPoints,rmEvictions,thrHalvings,thrIncreases,thrClamps,gateVetoCycles" > "$CSV"
fi

# run <sweep> <label> <compression> <l1_size> <assoc> <cacheline> <capacitance>
#     <n_thres> <aimd:0|1> <icache_comp:on|off> [reward_d] [reward_i]
#
# The rewards default to -1, i.e. ACC measures its own miss penalty. That is
# what keeps a sweep internally consistent: the reward is a property of the
# machine (geometry, workload, queueing), so a constant calibrated at one cell
# misprices every other one, and rows would differ by their calibration instead
# of by the policy under test. Pass them explicitly only to reproduce a frozen
# result that was calibrated by hand.
run () {
    local sweep=$1 label=$2 comp=$3 size=$4 assoc=$5 line=$6 cap=$7
    local nthres=$8 aimd=$9 icomp=${10}
    local rd=${11:--1} ri=${12:--1}

    local dir="$OUTROOT/$label"
    local extra=()

    if [ "$comp" = "kagura" ]; then
        extra+=(--n-thres "$nthres")
        [ "$aimd" = "0" ] && extra+=(--no-aimd)
    fi

    # NVM_TO_CAP=1 drains main-memory traffic energy from the capacitor rather
    # than only accounting it. Set per sweep, not per row, so a sweep is never
    # half on one energy model and half on the other.
    [ "${NVM_TO_CAP:-0}" = "1" ] && extra+=(--nvm-to-capacitor)

    # DIRTY_AWARE=1 exempts D-cache fills that serve a write from Kagura's
    # Regular Mode gate (the proposed repair of the >=2 kB self-defeat).
    [ "${DIRTY_AWARE:-0}" = "1" ] && extra+=(--dirty-aware)

    # CKPT_GATE=1 probes dirty blocks with a measurement-only BDI at each
    # checkpoint (ckptProbe* stats; behaviour unchanged). 'none' rows only --
    # the config fatals otherwise.
    [ "${CKPT_GATE:-0}" = "1" ] && extra+=(--ckpt-gate)

    # VECTOR_DUMP=1 additionally writes each probed dirty block into the run's
    # own output dir (vectors.txt): the golden-model corpus for the hardware
    # BDI testbenches. Needs the probe, so it is gated on CKPT_GATE too.
    [ "${VECTOR_DUMP:-0}" = "1" ] && [ "${CKPT_GATE:-0}" = "1" ] && \
        extra+=(--vector-dump "$dir/vectors.txt")

    # THRES_CAP bounds N_thres to this fraction of R_prev at each reboot
    # (the repair of the AIMD runaway regime -- see KaguraController.py).
    # Kagura rows only; unset or 0 keeps the published rule. Per-row is fine
    # here: unlike the energy model, this is a policy under test, so a sweep
    # may legitimately hold one row at the published rule and cap the next.
    local tcap=0
    if [ "$comp" = "kagura" ] && [ -n "${THRES_CAP:-}" ]; then
        tcap=$THRES_CAP
        extra+=(--thres-cap "$THRES_CAP")
    fi

    # RM_CONFIDENCE floors the 2-bit counter for entering Regular Mode (the
    # repair of the estimator-variance regime, where the cap is inert -- see
    # KaguraController.py). Kagura rows only; per-row for the same reason as
    # THRES_CAP: it is a policy under test, not an energy model.
    local rconf=0
    if [ "$comp" = "kagura" ] && [ -n "${RM_CONFIDENCE:-}" ]; then
        rconf=$RM_CONFIDENCE
        extra+=(--rm-confidence "$RM_CONFIDENCE")
    fi

    # SAT_INIT overrides the 2-bit counter's initial value (kagura rows).
    # Per sweep, like the energy knobs: it decides whether the R_adjust
    # spiral (see --sat-init in the config) is armed, and rows on opposite
    # sides of that compare estimator regimes, not policies.
    [ "$comp" = "kagura" ] && [ -n "${SAT_INIT:-}" ] && \
        extra+=(--sat-init "$SAT_INIT")

    # RM_PERCEPTRON=1 swaps the confidence-counter entry gate for the
    # perceptron. Setting it alongside RM_CONFIDENCE is legal and is its own
    # configuration: the vetoes become a union, either gate alone suppressing
    # the switch. PERC_HISTORY overrides H; PERC_VOLATILE=1 zeroes the
    # predictor at each reboot (unpersisted-learner control). Kagura rows
    # only, like the gate it sits beside.
    if [ "$comp" = "kagura" ] && [ "${RM_PERCEPTRON:-0}" = "1" ]; then
        extra+=(--rm-perceptron)
        [ -n "${PERC_HISTORY:-}" ] && extra+=(--perc-history "$PERC_HISTORY")
        [ "${PERC_VOLATILE:-0}" = "1" ] && extra+=(--perc-volatile)
    fi

    # E_NVM_READ / E_NVM_WRITE (J/B) override the placeholder NVM energies.
    # Set per sweep, like the energy model: every NVM energy figure is linear
    # in them, so rows priced differently compare nothing. See the config's
    # --e-nvm-read/--e-nvm-write help for the published bracket.
    [ -n "${E_NVM_READ:-}" ]  && extra+=(--e-nvm-read "$E_NVM_READ")
    [ -n "${E_NVM_WRITE:-}" ] && extra+=(--e-nvm-write "$E_NVM_WRITE")

    # E_PER_INST (J/inst) / P_STATIC (W) override the stage 2 core-energy
    # placeholders. Per sweep, like the NVM knobs: the core term is the bulk
    # of every row's drain, so rows priced differently compare nothing. See
    # the config's --e-per-inst/--p-static help for the paper-regime values.
    [ -n "${E_PER_INST:-}" ] && extra+=(--e-per-inst "$E_PER_INST")
    [ -n "${P_STATIC:-}" ]   && extra+=(--p-static "$P_STATIC")

    # TRACE_FILE switches the harvest source from the square wave to a
    # file-based power trace (TRACE_SCALE calibrates it). Set per sweep, like
    # the energy model: rows under different supply regimes compare nothing.
    [ -n "${TRACE_FILE:-}" ] && \
        extra+=(--trace-file "$TRACE_FILE" --trace-scale "${TRACE_SCALE:-1.0}")

    echo "=== $label"
    "$GEM5" --outdir="$dir" "$CFG" \
        --cmd "$BIN" --cwd "$BENCH_CWD" --options "$BENCH_OPTS" \
        --compression "$comp" \
        --l1-size "$size" --l1-assoc "$assoc" --cacheline "$line" \
        --capacitance "$cap" --icache-compression "$icomp" \
        --reward-cycles-dcache "$rd" --reward-cycles-icache "$ri" \
        "${extra[@]}" > "$dir.log" 2>&1

    local s="$dir/stats.txt"
    echo "$sweep,$label,$comp,$size,$assoc,$line,$cap,$nthres,$aimd,$icomp,${NVM_TO_CAP:-0},${DIRTY_AWARE:-0},$tcap,$rconf,${E_NVM_READ:-2e-12},${E_NVM_WRITE:-10e-12},${E_PER_INST:-80e-12},${P_STATIC:-0.5e-3},\
$(stat_of simTicks "$s"),\
$(stat_of intermittent.numPowerFailures "$s"),\
$(stat_of intermittent.compressionEnergy "$s"),\
$(stat_of intermittent.checkpointEnergy "$s"),\
$(stat_of intermittent.checkpointBytes "$s"),\
$(stat_of intermittent.checkpointDirtyBytes "$s"),\
$(stat_of intermittent.nvmEnergy "$s"),\
$(stat_of intermittent.nvmReadEnergy "$s"),\
$(stat_of intermittent.nvmWriteEnergy "$s"),\
$(stat_of intermittent.nvmReadBytes "$s"),\
$(stat_of intermittent.nvmWriteBytes "$s"),\
$(stat_of system.cpu.dcache.overallMissRate::total "$s"),\
$(stat_of system.cpu.icache.overallMissRate::total "$s"),\
$(stat_of system.cpu.dcache.avgMissPenaltyTicks "$s"),\
$(stat_of system.cpu.icache.avgMissPenaltyTicks "$s"),\
$(stat_of intermittent.memOps "$s"),\
$(stat_of intermittent.memOpsRegularMode "$s"),\
$(stat_of intermittent.decisionPoints "$s"),\
$(stat_of intermittent.rmEvictions "$s"),\
$(stat_of intermittent.thresholdHalvings "$s"),\
$(stat_of intermittent.thresholdIncreases "$s"),\
$(stat_of intermittent.thresholdClamps "$s"),\
$(stat_of intermittent.cyclesGateSuppressed "$s")" >> "$CSV"
}

# --- sweep 0: the frozen point ------------------------------------------------
#
# Two rows, and they answer different questions.
#
# regress-pinned passes the hand calibration (17 and 19 cycles) that the frozen
# stage 4/5 configs used, so it must reproduce the stage 5 result EXACTLY --
# simTicks 920713155000, 94 failures, compressionEnergy 1.552e-05. That is the
# check that the fork and the new knobs are inert at the old operating point.
#
# regress-auto runs the same point with the reward measured instead of given.
# It is not expected to be bit-identical; it is the validation that the
# measurement lands where the hand calibration did. Look at dRewardTicks and
# iRewardTicks: they should come out near 100116 and 108520 ticks (the stage 3
# avgMissLatency figures the constants were derived from). If they do not, one
# of the two methods is wrong and the sweeps are not yet trustworthy.
sweep_regress () {
    run regress "regress-pinned" kagura 256B 2 32 1e-6 8 1 on 17 19
    run regress "regress-auto"   kagura 256B 2 32 1e-6 8 1 on
}

# --- sweep 0b: does the energy model change the verdict? ----------------------
#
# The same four runs under both energy models: NVM traffic merely accounted
# (the default), and NVM traffic actually drawn from the capacitor.
#
# Feeding it back is the physically honest model, and it is not a refinement --
# it changes what is being asked. Power cycles shorten, so the failure count
# rises and the run takes longer. Kagura's Regular Mode tail is roughly N_thres
# operations out of a cycle, and N_thres is tuned on eviction pressure rather
# than on cycle length, so a SHORTER cycle means that tail covers a LARGER
# fraction of it: Kagura should save more. Against that, more failures means
# more checkpoints, and Kagura pays 17 B of register state at every one, so its
# counter-cost rises too. Which wins is the measurement.
#
# The implication if Kagura's saving grows: the unfed model, by understating
# consumption, gives power cycles that are too long, and has therefore been
# evaluating Kagura in the regime where it matters LEAST -- while the paper's
# whole premise is that cycles are short.
sweep_ledger () {
    NVM_TO_CAP=0 run ledger "ledger-acc-accounted"    acc    256B 2 32 1e-6 8 1 on
    NVM_TO_CAP=0 run ledger "ledger-kagura-accounted" kagura 256B 2 32 1e-6 8 1 on
    NVM_TO_CAP=1 run ledger "ledger-acc-drained"      acc    256B 2 32 1e-6 8 1 on
    NVM_TO_CAP=1 run ledger "ledger-kagura-drained"   kagura 256B 2 32 1e-6 8 1 on
}

# --- paper match: accounted ACC vs Kagura(AIMD), one benchmark --------------
#
# The single comparison Fig. 18 asks for, per benchmark: ACC alone against
# Kagura's published AIMD design, both under the ACCOUNTED energy model (NVM
# traffic post-processed, not drawn from the capacitor -- the paper's method,
# see the ledger sweep note). compEnergy is linear in the compression-op count,
# so (acc - kagura)/acc is our analogue of the paper's compression-reduction
# ratio. Override the workload with BIN / BENCH_CWD / BENCH_OPTS and give each
# benchmark its own OUTROOT so the rows never misalign against a stale header.
#
# The none row is the paper's Fig. 13 baseline (compressor-free
# NVSRAMCache): speedup = simTicks(none)/simTicks(x) - 1. Compression and
# checkpoint energy are always drawn from the capacitor, so the paper's
# speedup channel (useless compressions shorten power cycles) is live under
# this model too; only NVM stays post-processed, as the paper's does.
sweep_paper () {
    NVM_TO_CAP=0 run paper "paper-none"   none   256B 2 32 1e-6 0 1 on
    NVM_TO_CAP=0 run paper "paper-acc"    acc    256B 2 32 1e-6 0 1 on
    NVM_TO_CAP=0 run paper "paper-kagura" kagura 256B 2 32 1e-6 8 1 on
}

# Retrofit for OUTROOTs whose paper sweep predates the none row: appends just
# that row to the existing CSV (same columns, so appending is safe).
sweep_paperbase () {
    NVM_TO_CAP=0 run paper "paper-none"   none   256B 2 32 1e-6 0 1 on
}

# --- paper match, trace regime: the speedup experiment ------------------------
#
# The paper trio again, but under a bursty harvest trace and the paper's
# 4.7 uF capacitor (its Table I default; capacitor and trace are a package --
# per its Figs. 29/30 a big capacitor under the square wave only moves further
# from the short-cycle regime). Under the square wave, deaths arrive every
# wave period regardless of what the program spent -- failures = runtime/10 ms
# exactly, on every benchmark -- so energy Kagura saves cannot buy cycle
# length and the measured speedup vs none comes out slightly NEGATIVE
# (compression's timing cost is all that is left). Under a bursty trace the
# cycle boundary is energy-limited: saved energy delays the death, sometimes
# past a whole trough, and the paper's speedup channel opens.
#
# Same accounted energy model as the paper sweep, so rows are comparable:
# speedup = simTicks(trace-none)/simTicks(trace-x) - 1, against the paper's
# Fig. 13. TRACE_FILE overrides the bundled synthetic trace (e.g. with the
# real RFHome, same format), TRACE_SCALE recalibrates it.
sweep_trace () {
    local tf=${TRACE_FILE:-workloads/traces/rf_bursty.txt}
    TRACE_FILE=$tf NVM_TO_CAP=0 run trace "trace-none"   none   256B 2 32 4.7e-6 0 1 on
    TRACE_FILE=$tf NVM_TO_CAP=0 run trace "trace-acc"    acc    256B 2 32 4.7e-6 0 1 on
    TRACE_FILE=$tf NVM_TO_CAP=0 run trace "trace-kagura" kagura 256B 2 32 4.7e-6 8 1 on
}

# The trace trio with NVM traffic energy drained from the capacitor. The
# compfit control quantified what the accounted trio cannot show: a 254x
# D-miss reduction bought +0.03% wall time, because with NVM traffic priced
# but not drawn, wall time is total energy over mean harvest power and a miss
# costs no energy at all. A real EHS pays memory access energy from the same
# buffer as everything else; draining it re-couples miss traffic to cycle
# length (fewer misses -> less drain -> longer cycles -> fewer failures).
# Rows are NOT comparable to the accounted trios -- compare only within this
# sweep. Both pJ/B figures are placeholders until NVSim numbers land, and
# every gap here is linear in them.
sweep_traced () {
    local tf=${TRACE_FILE:-workloads/traces/rf_bursty.txt}
    TRACE_FILE=$tf NVM_TO_CAP=1 run traced "traced-none"   none   256B 2 32 4.7e-6 0 1 on
    TRACE_FILE=$tf NVM_TO_CAP=1 run traced "traced-acc"    acc    256B 2 32 4.7e-6 0 1 on
    TRACE_FILE=$tf NVM_TO_CAP=1 run traced "traced-kagura" kagura 256B 2 32 4.7e-6 8 1 on
}

# The AIMD repair experiment, under the traced regime where the runaway
# first showed a wall-clock price (traced compfit: Kagura 0.39% SLOWER than
# ACC, with 480 increases against 91 halvings and Regular Mode covering 47%
# of memory ops). Five rows: the ACC bound, the published rule, and the
# capped rule at three fractions of R_prev. Success looks like the capped
# rows closing on -- ideally passing -- tracedcap-acc, with thrClamps busy
# and dMissRate back near ACC's. Fresh OUTROOT required: this sweep added
# the thresCap and thrClamps columns to the CSV.
sweep_tracedcap () {
    local tf=${TRACE_FILE:-workloads/traces/rf_bursty.txt}
    TRACE_FILE=$tf NVM_TO_CAP=1 run tracedcap "tracedcap-acc"  acc    256B 2 32 4.7e-6 0 1 on
    TRACE_FILE=$tf NVM_TO_CAP=1 run tracedcap "tracedcap-aimd" kagura 256B 2 32 4.7e-6 8 1 on
    for f in 0.05 0.1 0.25; do
        TRACE_FILE=$tf NVM_TO_CAP=1 THRES_CAP=$f run tracedcap \
            "tracedcap-$f" kagura 256B 2 32 4.7e-6 8 1 on
    done
}

# The confidence-gate experiment, under paper-regime core energies -- the
# regime where the stakes are measured: on compfit the published rule costs
# Kagura -0.59% against ACC (Regular Mode swallows ~48% of memory ops off a
# broken one-cycle estimate) and the threshold cap was tick-identical to
# published, proving the defect is the estimator, not the threshold. Five
# rows bracket the repair: the uncompressed baseline and ACC bound the
# ordering; then the published rule and the gate at 2 (enter Regular Mode
# only on demonstrated estimate reliability) and 3 (strictest). Success is
# a gated row landing between acc and none -- the full kagura > acc > none
# ordering -- with gateVetoCycles busy and dMissRate back at ACC's. On
# benchmarks where AIMD already behaves (sha, fft, basicmath) the gate
# should cost little: their estimates earn the confidence it demands.
sweep_tracedgate () {
    local tf=${TRACE_FILE:-workloads/traces/rf_bursty.txt}
    local ei=${E_PER_INST:-22e-12} ps=${P_STATIC:-50e-6}
    TRACE_FILE=$tf NVM_TO_CAP=1 E_PER_INST=$ei P_STATIC=$ps \
        run tracedgate "tracedgate-none" none   256B 2 32 4.7e-6 0 1 on
    TRACE_FILE=$tf NVM_TO_CAP=1 E_PER_INST=$ei P_STATIC=$ps \
        run tracedgate "tracedgate-acc"  acc    256B 2 32 4.7e-6 0 1 on
    TRACE_FILE=$tf NVM_TO_CAP=1 E_PER_INST=$ei P_STATIC=$ps \
        run tracedgate "tracedgate-pub"  kagura 256B 2 32 4.7e-6 8 1 on
    for c in 2 3; do
        TRACE_FILE=$tf NVM_TO_CAP=1 E_PER_INST=$ei P_STATIC=$ps \
            RM_CONFIDENCE=$c \
            run tracedgate "tracedgate-c$c" kagura 256B 2 32 4.7e-6 8 1 on
    done
}

# The perceptron entry gate against its rivals, all in one OUTROOT so the
# margins are computed under identical pricing: none/acc anchor the scale,
# pub is the published rule (no gate), c2 is the 2-bit counter floor, perc
# is the perceptron (persistent, checkpoint-priced), percvol is the
# unpersisted control (weights die at each failure -- should be
# tick-identical to pub while charging nothing extra, since a zeroed
# perceptron predicts y=0 and the gate stays open). The interesting
# comparisons: perc vs c2 (does learned pattern memory beat a saturating
# counter on this harvest?) and perc vs pub (does any gate pay?). Bursty
# trace by default -- the estimator's worst case, where c2 earned its
# do-no-harm result; override TRACE_FILE for the periodic (adaptive) check.
sweep_percgate () {
    local tf=${TRACE_FILE:-workloads/traces/rf_bursty.txt}
    local ei=${E_PER_INST:-22e-12} ps=${P_STATIC:-50e-6}
    TRACE_FILE=$tf NVM_TO_CAP=1 E_PER_INST=$ei P_STATIC=$ps \
        run percgate "percgate-none" none   256B 2 32 4.7e-6 0 1 on
    TRACE_FILE=$tf NVM_TO_CAP=1 E_PER_INST=$ei P_STATIC=$ps \
        run percgate "percgate-acc"  acc    256B 2 32 4.7e-6 0 1 on
    TRACE_FILE=$tf NVM_TO_CAP=1 E_PER_INST=$ei P_STATIC=$ps \
        run percgate "percgate-pub"  kagura 256B 2 32 4.7e-6 8 1 on
    TRACE_FILE=$tf NVM_TO_CAP=1 E_PER_INST=$ei P_STATIC=$ps \
        RM_CONFIDENCE=2 \
        run percgate "percgate-c2"   kagura 256B 2 32 4.7e-6 8 1 on
    TRACE_FILE=$tf NVM_TO_CAP=1 E_PER_INST=$ei P_STATIC=$ps \
        RM_CONFIDENCE=3 \
        run percgate "percgate-c3"   kagura 256B 2 32 4.7e-6 8 1 on
    TRACE_FILE=$tf NVM_TO_CAP=1 E_PER_INST=$ei P_STATIC=$ps \
        RM_PERCEPTRON=1 \
        run percgate "percgate-perc" kagura 256B 2 32 4.7e-6 8 1 on
    TRACE_FILE=$tf NVM_TO_CAP=1 E_PER_INST=$ei P_STATIC=$ps \
        RM_PERCEPTRON=1 PERC_VOLATILE=1 \
        run percgate "percgate-percvol" kagura 256B 2 32 4.7e-6 8 1 on
}

# The traced trio under paper-regime core energies. The traced results
# showed the Kagura-vs-ACC gap is a fixed compression-energy quantum divided
# by total drain, and ~97% of our drain is the core term -- which is still
# the stage 2 placeholder (80 pJ/inst, 0.5 mW), not a paper number. Table I
# prices the SRAM access at 9 pJ and the core is 45 nm McPAT-LOP, which puts
# the paper-faithful core figure near 22 pJ/inst and ~50 uW leakage (see the
# config's --e-per-inst/--p-static help for the derivation). Same trio,
# recalibrated denominator: the gaps should scale by roughly the core-energy
# ratio, and whatever remains vs the paper's +4.74% is then attributable to
# data compressibility, not to energy pricing. NVM stays at the default
# placeholder unless E_NVM_READ/E_NVM_WRITE are set. Fresh OUTROOT required:
# this sweep added the ePerInst and pStatic columns to the CSV.
sweep_tracedpaper () {
    local tf=${TRACE_FILE:-workloads/traces/rf_bursty.txt}
    local ei=${E_PER_INST:-22e-12} ps=${P_STATIC:-50e-6}
    TRACE_FILE=$tf NVM_TO_CAP=1 E_PER_INST=$ei P_STATIC=$ps \
        run tracedpaper "tracedpaper-none"   none   256B 2 32 4.7e-6 0 1 on
    TRACE_FILE=$tf NVM_TO_CAP=1 E_PER_INST=$ei P_STATIC=$ps \
        run tracedpaper "tracedpaper-acc"    acc    256B 2 32 4.7e-6 0 1 on
    TRACE_FILE=$tf NVM_TO_CAP=1 E_PER_INST=$ei P_STATIC=$ps \
        run tracedpaper "tracedpaper-kagura" kagura 256B 2 32 4.7e-6 8 1 on
}

# The same trio at 4 ways. The geom sweep showed CompressedTags halves
# effective associativity (bdi-a4 lands on none-a2 within 0.06%), and that
# timing debt is why trace-kagura still loses to trace-none at 2 ways even
# with the energy channel open. Doubling ways neutralizes the artifact with
# existing machinery: if kagura beats none here, the end-to-end speedup sign
# is demonstrated, and what remains vs the paper is organization, not
# mechanism. none-a4 is the baseline so the comparison stays like-for-like.
sweep_trace4 () {
    local tf=${TRACE_FILE:-workloads/traces/rf_bursty.txt}
    TRACE_FILE=$tf NVM_TO_CAP=0 run trace "trace4-none"   none   256B 4 32 4.7e-6 0 1 on
    TRACE_FILE=$tf NVM_TO_CAP=0 run trace "trace4-acc"    acc    256B 4 32 4.7e-6 0 1 on
    TRACE_FILE=$tf NVM_TO_CAP=0 run trace "trace4-kagura" kagura 256B 4 32 4.7e-6 8 1 on
}

# --- sweep 1: cache size x policy -------------------------------------------
#
# The crossover test. Checkpoint energy is paid per dirty block, so it grows
# with the cache; compression energy is paid per fill, so it shrinks with it.
# At 256 B compression outweighs checkpointing 100:1. Somewhere above, the two
# cross -- and above THAT, Kagura is optimising the smaller term.
sweep_size () {
    for size in 256B 512B 1kB 2kB 4kB 8kB 16kB; do
        for comp in none bdi acc kagura; do
            run size "size-$size-$comp" "$comp" "$size" 2 32 1e-6 8 1 on
        done
    done
}

# --- sweep 2: pinned N_thres (AIMD off) --------------------------------------
#
# Stage 5 showed AIMD saturated: R_evict (thousands) is compared against
# R_thres/2 (single digits), so every decision cycle halved and the threshold
# never climbed. Pinning it traces the energy-versus-miss-rate curve the rule
# is searching. A power cycle is ~89k memory ops at 1 uF, so the top of this
# range disables compression for roughly a third of the cycle.
#
# The two reference rows are the operating points to place ON that curve: the
# published AIMD rule, and ACC without Kagura at all.
sweep_thres () {
    run thres "thres-acc-ref"   acc    256B 2 32 1e-6 0     1 on
    run thres "thres-aimd-ref"  kagura 256B 2 32 1e-6 8     1 on
    for n in 8 32 128 512 2048 8192 16384 32768; do
        run thres "thres-$n" kagura 256B 2 32 1e-6 "$n" 0 on
    done
}

# --- sweep 3: attribution -----------------------------------------------------
#
# All of Stage 5's saving came from gating I-cache fills, because ACC had
# already switched the D-cache compressor off. So: does Kagura beat the trivial
# policy of never compressing the I-cache in the first place? If not, the
# benefit belongs to a repair of ACC's predictor, not to intermittence-
# awareness -- and the mechanism only earns its keep where the D-cache
# compressor is actually live, which is what the larger cache rows test.
sweep_attrib () {
    for size in 256B 4kB; do
        run attrib "attrib-$size-acc"        acc    "$size" 2 32 1e-6 8 1 on
        run attrib "attrib-$size-acc-noic"   acc    "$size" 2 32 1e-6 8 1 off
        run attrib "attrib-$size-kagura"     kagura "$size" 2 32 1e-6 8 1 on
        run attrib "attrib-$size-kagura-noic" kagura "$size" 2 32 1e-6 8 1 off
    done
}

# --- sweep 4: tag geometry ----------------------------------------------------
#
# Why does the compressed I-cache miss MORE than the uncompressed one at every
# size >= 512 B? Suspect: not compression, but the tag organization it rides
# on. CompressedTags is YACC-style (Sardashti et al. 2016): the set index moves
# up one bit (superblocks are 2 blocks wide), and two neighbouring 32 B blocks
# share one way of a set unless BOTH compress to <= 16 B. Code essentially
# never BDI-compresses, so straight-line code claims BOTH ways of its set --
# a 2-way compressed cache behaves direct-mapped at 64 B granularity for
# instruction streams. ACC's original organization (VSC, per-block tags)
# degrades to the conventional geometry instead and would not show this.
#
# The controls, all at 8 kB (the worst ratio, 11.6% vs 2.5%), bdi so no GCP
# dynamics, drained to match the size sweep rows:
#   none-a2 / bdi-a2   reproduce the anomaly in-place
#   bdi-a4             halved effective associativity restored by doubling
#                      ways: should land near none-a2 if geometry is the cause
#   none-a4            shows the a4 gain is not generic (none-a2 is already
#                      past its conflict knee)
#   none-a1            conventional direct-mapped: should land near bdi-a2
sweep_geom () {
    NVM_TO_CAP=1 run geom "geom-8kB-none-a2" none 8kB 2 32 1e-6 8 1 on
    NVM_TO_CAP=1 run geom "geom-8kB-bdi-a2"  bdi  8kB 2 32 1e-6 8 1 on
    NVM_TO_CAP=1 run geom "geom-8kB-bdi-a4"  bdi  8kB 4 32 1e-6 8 1 on
    NVM_TO_CAP=1 run geom "geom-8kB-none-a4" none 8kB 4 32 1e-6 8 1 on
    NVM_TO_CAP=1 run geom "geom-8kB-none-a1" none 8kB 1 32 1e-6 8 1 on
}

# --- sweep 5: dirty-aware Regular Mode -----------------------------------------
#
# The size sweep's verdict was that Kagura defeats itself above ~2 kB: Regular
# Mode fills dirty blocks uncompressed, those blocks ARE the next checkpoint,
# and the inflated checkpoint costs more than the gated compressions saved
# (+139 kB of dirty bytes against +9.3 kB of register state at 16 kB). The
# gate's own argument only holds for clean blocks, which die free with the
# SRAM. The repair: exempt fills that serve a write, i.e. keep compressing
# what will have to be written back anyway.
#
# One kagura row per size, drained, same geometry as the size sweep -- compare
# each against its size-<n>-kagura and size-<n>-acc rows. Success looks like:
# ckptBytes back at or below ACC's, the small-cache wins intact (those came
# from clean I-fills, which stay gated), and the >=2 kB net losses gone.
# dirtyOverrides in stats.txt says how often the exemption actually fired.
#
# Run with a fresh OUTROOT: this sweep's CSV has the dirtyAware column, and
# rows appended to a pre-existing results.csv would misalign against its
# header.
sweep_dirty () {
    for size in 256B 512B 1kB 2kB 4kB 8kB 16kB; do
        DIRTY_AWARE=1 NVM_TO_CAP=1 run dirty "dirty-$size-kagura" \
            kagura "$size" 2 32 1e-6 8 1 on
    done
}

# --- sweep: where does an inference's energy actually go? ---------------------
#
# The one number the TinyML direction rests on and no published source gives:
# the share of inference energy spent fetching weights from NVM. SONIC/TAILS
# reports only overhead terms (26% control, 14% loop-index FRAM writes, ~40%
# fetch/decode) and never splits weight loads out, so it cannot validate or
# refute the estimate. This measures it here.
#
# The axis is `reuse` -- MACs per weight byte -- because that ratio, not the
# absolute traffic, decides the answer, and it is fixed by the layer type
# rather than free to choose: 1 is fully-connected at batch=1 (the anomaly
# autoencoder), 36 is a 1x1 conv on a 6x6 map (MobileNet's late layers), 100s
# are early convs. Weight traffic per inference is constant at 64 kB while
# compute scales with reuse, so the sweep traces the compute:traffic ratio and
# the answer comes out as a curve over the range real models occupy.
#
# Inferences are reduced as reuse rises to keep runtime bounded; the report
# normalises per inference and reports fractions, so the rows stay comparable.
#
# Drained (NVM_TO_CAP=1), because the question is what the weight fetch costs
# the capacitor, not what it costs an unfed ledger. Uncompressed cache
# (--compression none): the weight stream is dead on arrival, so cache
# compression has nothing to offer it, and mixing it in would confound the
# decomposition with a mechanism this measurement is not about.
#
# Same environment as tracedpaper, and for the same two reasons -- a fetch
# share measured under a different one would not be comparable with anything
# else in the campaign, and both defaults are actively wrong here:
#
#   E_PER_INST=22e-12, P_STATIC=50e-6 (Table I regime, not the 80 pJ/0.5 mW
#   stage-2 placeholders). The core term is the denominator of the fetch
#   share, so the placeholder would inflate core energy ~3.6x and understate
#   the weight fetch by the same factor -- an error against the hypothesis,
#   which is the wrong direction to be sloppy in when the answer is a share.
#
#   TRACE_FILE=rf_bursty. Under the square wave a power failure arrives every
#   period regardless of what the program spent, so saved energy cannot buy
#   cycle length: the wave-limited regime converts a saving into exactly zero
#   extra inferences by construction. Measuring "what would compressed weights
#   buy" there would answer the question with an artefact of the supply.
#
# Read the result with: python3 configs/kagura/wstream_report.py
sweep_wstream () {
    BIN=workloads/bin/wstream
    BENCH_CWD=.
    local tf=${TRACE_FILE:-workloads/traces/rf_bursty.txt}
    local ei=${E_PER_INST:-22e-12} ps=${P_STATIC:-50e-6}

    #        reuse  inferences
    for pair in "1 4" "4 4" "16 2" "36 2" "64 1" "144 1"; do
        set -- $pair
        local reuse=$1 infs=$2
        BENCH_OPTS="$reuse $infs" NVM_TO_CAP=1 \
            TRACE_FILE=$tf E_PER_INST=$ei P_STATIC=$ps \
            run wstream "wstream-r$reuse" none 256B 2 32 4.7e-6 0 1 on
    done
}

# --- sweep: is the dirty set compressible at all? -----------------------------
#
# The go/no-go gate for checkpoint-time compression, run BEFORE building the
# mechanism. The fill-stream audit measured what MiBench READS (88-100%
# incompressible under BDI); nobody has measured what it WROTE -- the dirty
# blocks a checkpoint drains. Plausibly a different population (stack frames,
# zeroed buffers, small integers); possibly not. One day of measurement
# decides five weeks of build.
#
# Sizes are the crossover regime. At 256 B a checkpoint is ~200 B, so any
# ratio is immaterial in absolute terms; checkpoint energy only becomes a
# first-order term at the >=2 kB sizes where ungated Kagura self-defeated.
# 256 B is kept for continuity with the paper's operating point.
#
# Controls come FIRST and bracket the instrument: dirtyzero must read near
# BDI's zero-line ceiling, dirtyrand must read ~1.0x. A probe that fails
# either invalidates every benchmark row after it. Controls run at three
# sizes only -- they validate the probe, not the policy.
#
# Environment: drained + square wave like sweep_dirty (comparable dirty-set
# populations), but Table I core pricing (22 pJ/inst, 50 uW) like tracedpaper
# -- the materiality verdict divides by total drain, and the stage-2 core
# placeholder would inflate that denominator ~3.6x and understate the saving,
# the wrong direction to be sloppy in for a go/no-go number. The RATIO is a
# data property and does not care; only materiality does.
#
# Pre-registered thresholds (set before any run; evaluated on the benchmark
# rows at >=2 kB): ratio >=1.5x AND projected saving >=1% of total drain at
# some size => proceed to the mechanism; ratio <=1.1x everywhere => dirty set
# is the fill stream's population, falsified, pivot to model-grounding;
# between => decide on the distribution (>=2x blocks may carry a selective
# scheme).
#
# Read the result with: python3 configs/kagura/ckptgate_report.py
sweep_ckptgate () {
    local ei=${E_PER_INST:-22e-12} ps=${P_STATIC:-50e-6}

    # label:binary:cwd:options
    local controls=(
        "dirtyzero:workloads/bin/dirtyzero:.:"
        "dirtyrand:workloads/bin/dirtyrand:.:"
    )
    local benches=(
        "qsort:workloads/bin/qsort_small:workloads/mibench/automotive/qsort:input_small.dat"
        "sha:workloads/bin/sha:workloads/mibench/security/sha:input_small.asc"
        "fft:workloads/bin/fft:workloads/mibench/telecomm/FFT:4 4096"
    )

    local entry label bin cwd opts size
    for entry in "${controls[@]}"; do
        IFS=: read -r label bin cwd opts <<< "$entry"
        for size in 1kB 4kB 16kB; do
            BIN=$bin BENCH_CWD=$cwd BENCH_OPTS=$opts \
                CKPT_GATE=1 NVM_TO_CAP=1 E_PER_INST=$ei P_STATIC=$ps \
                run ckptgate "ckptgate-$size-$label" none "$size" 2 32 1e-6 8 1 on
        done
    done

    for entry in "${benches[@]}"; do
        IFS=: read -r label bin cwd opts <<< "$entry"
        for size in 256B 1kB 2kB 4kB 8kB 16kB; do
            BIN=$bin BENCH_CWD=$cwd BENCH_OPTS=$opts \
                CKPT_GATE=1 NVM_TO_CAP=1 E_PER_INST=$ei P_STATIC=$ps \
                run ckptgate "ckptgate-$size-$label" none "$size" 2 32 1e-6 8 1 on
        done
    done
}

case "${1:-all}" in
    regress) sweep_regress ;;
    wstream) sweep_wstream ;;
    ckptgate) sweep_ckptgate ;;
    paper)   sweep_paper ;;
    paperbase) sweep_paperbase ;;
    trace)   sweep_trace ;;
    traced)  sweep_traced ;;
    tracedcap) sweep_tracedcap ;;
    tracedpaper) sweep_tracedpaper ;;
    tracedgate) sweep_tracedgate ;;
    percgate) sweep_percgate ;;
    trace4)  sweep_trace4 ;;
    ledger)  sweep_ledger ;;
    size)    sweep_size ;;
    thres)   sweep_thres ;;
    attrib)  sweep_attrib ;;
    geom)    sweep_geom ;;
    dirty)   sweep_dirty ;;
    all)     sweep_regress; sweep_ledger; sweep_thres; sweep_attrib; sweep_size ;;
    *)       echo "usage: bash $0 [regress|paper|paperbase|trace|traced|tracedcap|trace4|ledger|size|thres|attrib|geom|dirty|wstream|ckptgate|percgate|all]" >&2
             exit 1 ;;
esac

echo
echo "done -> $CSV"
