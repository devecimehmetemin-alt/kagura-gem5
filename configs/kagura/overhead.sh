#!/usr/bin/env bash
#
# Kagura decision-unit overhead sweep.
#
# The paper prices its mechanism once, as CACTI area for 162 bits (Sec.
# VIII-A). On an EHS that is not the whole bill: those registers are
# architectural state, so every power failure writes them to NVM again. This
# sweep measures that recurring cost as the predictor's state grows, against
# the benefit the predictor actually delivers.
#
# The perceptron axis is the interesting one because --perc-volatile splits it
# cleanly: persistent weights are checkpointed and cost energy, volatile
# weights are free and learn nothing. Same predictor, both ends of the trade.
#
# Run from the gem5 root, after generating the harvest trace:
#
#   (cd workloads/traces && python3 make_rfhome_trace.py)
#   bash configs/kagura/overhead.sh
#
# Results land in overhead/results.csv, full gem5 output under overhead/<label>/.
set -euo pipefail

GEM5=${GEM5:-./build/ARM/gem5.opt}
CFG=${CFG:-configs/kagura/stage_six_sweep.py}

BIN=${BIN:-workloads/bin/qsort_small}
BENCH_CWD=${BENCH_CWD:-workloads/mibench/automotive/qsort}
BENCH_OPTS=${BENCH_OPTS:-input_small.dat}

# Table I, as reproduced in results_paper_scale.md. The core figures are the
# back-derived LOP ones from 7.8, not the placeholders.
TRACE=${TRACE:-workloads/traces/rfhome.txt}
E_PER_INST=${E_PER_INST:-22e-12}
P_STATIC=${P_STATIC:-50e-6}

COMMON="--l1-size 256 --l1-assoc 2 --cacheline 32
        --capacitance 4.7e-6 --v-on 2.4 --v-off 1.8
        --trace-file $TRACE --nvm-to-capacitor
        --e-per-inst $E_PER_INST --p-static $P_STATIC"

OUTROOT=${OUTROOT:-overhead}
CSV=$OUTROOT/results.csv
mkdir -p "$OUTROOT"

stat_of () {
    awk -v key="$1" '$1 == key { print $2; exit }' "$2"
}

if [ ! -f "$CSV" ]; then
    echo "label,compression,percH,volatile,simTicks,failures,\
ckptEnergy,ckptBytes,ckptDirtyBytes,regBytesPerCkpt,compEnergy,\
decisionPoints,gateVetoCycles,percMispredicts,percUpdates" > "$CSV"
fi

run () {
    local label=$1; shift
    local comp=$1; shift
    local percH=$1; shift
    local vol=$1; shift
    local dir="$OUTROOT/$label"
    mkdir -p "$dir"

    echo "== $label"
    $GEM5 -d "$dir" "$CFG" \
        --cmd "$BIN" --cwd "$BENCH_CWD" --options "$BENCH_OPTS" \
        --compression "$comp" $COMMON "$@" > "$dir/run.log" 2>&1

    local s="$dir/stats.txt"
    # Register bytes per checkpoint are a config constant, not a stat, so
    # recover them from the ledger: total minus dirty, over the failure
    # count. This also cross-checks CKPT_REG_BYTES against what was charged.
    local reg
    reg=$(awk -v b="$(stat_of intermittent.checkpointBytes "$s")" \
              -v d="$(stat_of intermittent.checkpointDirtyBytes "$s")" \
              -v f="$(stat_of intermittent.numPowerFailures "$s")" \
              'BEGIN { if (f > 0) printf "%.2f", (b - d) / f; else print "" }')

    echo "$label,$comp,$percH,$vol,\
$(stat_of simTicks "$s"),\
$(stat_of intermittent.numPowerFailures "$s"),\
$(stat_of intermittent.checkpointEnergy "$s"),\
$(stat_of intermittent.checkpointBytes "$s"),\
$(stat_of intermittent.checkpointDirtyBytes "$s"),\
$reg,\
$(stat_of intermittent.compressionEnergy "$s"),\
$(stat_of intermittent.decisionPoints "$s"),\
$(stat_of intermittent.cyclesGateSuppressed "$s"),\
$(stat_of intermittent.percMispredicts "$s"),\
$(stat_of intermittent.percUpdates "$s")" >> "$CSV"
}

# References: no decision unit at all, then the published rule, then the
# 2-bit confidence gate the perceptron has to beat.
run none none         "" ""
run acc  acc          "" ""
run kagura-pub kagura "" ""
run kagura-c2  kagura "" "" --rm-confidence 2

# The perceptron axis. Persistent weights are checkpointed every cycle;
# volatile ones are not, which is the same predictor with its overhead
# removed and its learning removed with it.
for H in 1 2 4 8 16 32 64; do
    run "perc-h$H"     kagura "$H" 0 --rm-perceptron --perc-history "$H"
    run "perc-h$H-vol" kagura "$H" 1 --rm-perceptron --perc-history "$H" \
        --perc-volatile
done

echo
echo "wrote $CSV"
column -s, -t "$CSV"
