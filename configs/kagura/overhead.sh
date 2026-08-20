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
# One workload, or a subset:
#
#   WORKLOADS=qsort bash configs/kagura/overhead.sh
#   WORKLOADS="jpegd jpege" bash configs/kagura/overhead.sh
#
# Results land in overhead/results.csv, gem5 output under overhead/<bench>/<label>/.
set -euo pipefail

GEM5=${GEM5:-./build/ARM/gem5.opt}
CFG=${CFG:-configs/kagura/stage_six_sweep.py}

# The paper-scale suite, minus the apps this tree cannot build. Rows whose
# binary is missing are skipped rather than fatal, so a partial workloads
# build still produces a usable table.
WORKLOADS=${WORKLOADS:-"qsort basicmath sha fft jpegd jpege gsme"}

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

# Per-workload invocation. BENCH_INPUT is stdin, which only gsm needs: gem5 SE
# has no statx, so toast's regular-file check fails on a named input.
# jpege and jpegd share a directory, so both write to /dev/null or parallel
# variants clobber each other's output.
spec_for () {
    BENCH_INPUT=""
    case $1 in
        qsort)
            BIN=workloads/bin/qsort_small
            BENCH_CWD=workloads/mibench/automotive/qsort
            BENCH_OPTS="input_small.dat" ;;
        basicmath)
            BIN=workloads/bin/basicmath_small
            BENCH_CWD=workloads/mibench/automotive/basicmath
            BENCH_OPTS="" ;;
        sha)
            BIN=workloads/bin/sha
            BENCH_CWD=workloads/mibench/security/sha
            BENCH_OPTS="input_large.asc" ;;
        fft)
            BIN=workloads/bin/fft
            BENCH_CWD=workloads/mibench/telecomm/FFT
            BENCH_OPTS="8 32768" ;;
        jpegd)
            BIN=workloads/bin/djpeg
            BENCH_CWD=workloads/mibench/consumer/jpeg
            BENCH_OPTS="-dct int -ppm -outfile /dev/null input_large.jpg" ;;
        jpege)
            BIN=workloads/bin/cjpeg
            BENCH_CWD=workloads/mibench/consumer/jpeg
            BENCH_OPTS="-dct int -progressive -opt -outfile /dev/null input_large.ppm" ;;
        gsme)
            BIN=workloads/bin/toast
            BENCH_CWD=workloads/mibench/telecomm/gsm
            BENCH_OPTS="-fps -c"
            BENCH_INPUT="data/large.au" ;;
        *)
            echo "unknown workload: $1" >&2; return 1 ;;
    esac
}

stat_of () {
    awk -v key="$1" '$1 == key { print $2; exit }' "$2"
}

if [ ! -f "$CSV" ]; then
    echo "bench,label,compression,percH,volatile,simTicks,failures,\
ckptEnergy,ckptBytes,ckptDirtyBytes,regBytesPerCkpt,compEnergy,\
decisionPoints,gateVetoCycles,percMispredicts,percUpdates" > "$CSV"
fi

run () {
    local bench=$1; shift
    local label=$1; shift
    local comp=$1; shift
    local percH=$1; shift
    local vol=$1; shift
    local dir="$OUTROOT/$bench/$label"
    mkdir -p "$dir"

    echo "== $bench/$label"
    local input_arg=()
    [ -n "$BENCH_INPUT" ] && input_arg=(--input "$BENCH_INPUT")

    $GEM5 -d "$dir" "$CFG" \
        --cmd "$BIN" --cwd "$BENCH_CWD" --options "$BENCH_OPTS" \
        "${input_arg[@]}" \
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

    echo "$bench,$label,$comp,$percH,$vol,\
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

for w in $WORKLOADS; do
    spec_for "$w"
    if [ ! -x "$BIN" ]; then
        echo "-- skipping $w: $BIN not built"
        continue
    fi
    echo
    echo "#### $w"

    # References: no decision unit at all, then the published rule, then the
    # 2-bit confidence gate the perceptron has to beat.
    run "$w" none       none   "" ""
    run "$w" acc        acc    "" ""
    run "$w" kagura-pub kagura "" ""
    run "$w" kagura-c2  kagura "" "" --rm-confidence 2

    # The perceptron axis. Persistent weights are checkpointed every cycle;
    # volatile ones are not, which is the same predictor with its overhead
    # removed and its learning removed with it.
    for H in 1 2 4 8 16 32 64; do
        run "$w" "perc-h$H"     kagura "$H" 0 --rm-perceptron --perc-history "$H"
        run "$w" "perc-h$H-vol" kagura "$H" 1 --rm-perceptron --perc-history "$H" \
            --perc-volatile
    done
done

echo
echo "wrote $CSV"
column -s, -t "$CSV"
