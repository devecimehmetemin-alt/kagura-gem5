#!/usr/bin/env bash
#
# Bound the sampling confound on the Kagura-vs-ACC margin.
#
# WHY THIS EXISTS
#
# ACC's predictor is fed by hits on co-allocated compressed blocks. In Regular
# Mode nothing co-allocates, so no reward can arrive and the counter would wedge
# at its floor forever -- the original design escapes this because its tags are
# decoupled from data and keep measuring for free, and gem5's are not. The
# escape hatch is sample_interval: compress every Nth gated fill anyway, at 1/N
# of the energy.
#
# That hatch is OURS, not the paper's. And ACC::compress() checks Kagura's
# override BEFORE the sampler (acc.cc:80 vs acc.cc:96), so a Kagura row also
# suppresses the forced compressions. Part of every measured Kagura-over-ACC
# saving is therefore the removal of an artifact we introduced, not a saving
# against the paper's ACC.
#
# The test: sweep N and watch the margin.
#
#   margin flat in N        -> the confound is bounded, quote the margin
#   margin scales like 1/N  -> the margin is (partly) our artifact, and the
#                              headline Kagura-vs-ACC number needs restating
#
# N = 0 is the limiting case: no sampling at all, so Kagura has nothing extra
# to suppress and the margin is confound-free by construction. If the N=0
# margin matches the N=64 margin, the question is settled outright.
#
# USAGE (from the gem5 root)
#
#   bash mSAMP.sh                     # burst5, d912 -- the largest Kagura margin
#   SHAPE=burst bash mSAMP.sh         # another shape
#   TRACE_SUFFIX=e40 bash mSAMP.sh    # the clean no-overflow density
#
# Leaves sweep-samp/<label>/ per run and sweep-samp/results.csv.

set -euo pipefail

GEM5=${GEM5:-./build/ARM/gem5.opt}
CFG=${CFG:-configs/kagura/stage_six_sweep.py}

# burst5 at d912 carries the largest published-Kagura penalty in the matrix
# (+1.7179% vs ACC), so it is where a sampling artifact would show up first.
WORKLOAD=${WORKLOAD:-compfit_stream}
SHAPE=${SHAPE:-burst5}
TRACE_SUFFIX=${TRACE_SUFFIX:-d912}

# Derive the workload and trace from an existing matrix row rather than
# restating them. These eight rows only mean something against the 321 already
# on disk, so anything that could drift between them (binary, argv, cwd, trace
# path) is read from the row they will be compared with instead of guessed.
REF=${REF:-sweep-matrix/${WORKLOAD}-${SHAPE}${TRACE_SUFFIX}-acc}
if [ ! -f "$REF/config.ini" ]; then
    echo "ABORT: no reference row at '$REF/config.ini'." >&2
    echo "Pick one with: ls -d sweep-matrix/*${SHAPE}${TRACE_SUFFIX}* " >&2
    echo "then re-run with REF=<that dir> bash mSAMP.sh" >&2
    exit 1
fi

# config.ini stores the Process cmd as the full argv on one line.
REF_CMD=$(awk -F= '/^cmd=/ { sub(/^cmd=/, ""); print; exit }' "$REF/config.ini")
BIN=${BIN:-$(echo "$REF_CMD" | awk '{print $1}')}
BENCH_OPTS=${BENCH_OPTS:-$(echo "$REF_CMD" | cut -s -d' ' -f2-)}
BENCH_CWD=${BENCH_CWD:-$(awk -F= '/^cwd=/ { print $2; exit }' "$REF/config.ini")}
TRACE_FILE=${TRACE_FILE:-$(awk -F= '/^trace_file=/ { print $2; exit }' "$REF/config.ini")}

# The four points. 0 is the confound-free limit; 64 is the default every
# existing row was run at; 16 and 256 bracket it by 4x each way.
INTERVALS=${INTERVALS:-"0 16 64 256"}

OUTROOT=${OUTROOT:-sweep-samp}
CSV="$OUTROOT/results.csv"
mkdir -p "$OUTROOT"

if [ ! -f "$TRACE_FILE" ]; then
    echo "ABORT: trace '$TRACE_FILE' not found" >&2
    exit 1
fi
if [ ! -x "$GEM5" ]; then
    echo "ABORT: '$GEM5' not built" >&2
    exit 1
fi
# The flag has to exist on both sides or every row silently runs at 64 and the
# sweep looks perfectly flat for the wrong reason. This is the same class of
# check as the failed-penalty gate: a missing knob produces a completed run.
if ! grep -q "sample-interval" "$CFG"; then
    echo "ABORT: $CFG has no --sample-interval flag (config edit not applied)" >&2
    exit 1
fi
if ! grep -q "sample_interval" build/ARM/params/ACC.hh 2>/dev/null; then
    echo "ABORT: build/ARM/params/ACC.hh has no sample_interval (rebuild gem5)" >&2
    exit 1
fi

echo "reference row : $REF"
echo "binary        : $BIN ${BENCH_OPTS:-(no argv)}"
echo "cwd           : ${BENCH_CWD:-(none)}"
echo "trace         : $TRACE_FILE"
echo "intervals     : $INTERVALS"
echo

stat_of () {
    local name=$1 file=$2
    [ -f "$file" ] || { echo ""; return; }
    awk -v n="$name" '$1 == n { print $2; exit }' "$file"
}

if [ ! -f "$CSV" ]; then
    echo "shape,trace,sample_interval,comp,simTicks,failures,decisionPoints,\
gatedCompressions,sampledCompressions,compressionsRun_d,compressionEnergy,\
dmissrate" > "$CSV"
fi

run () {
    local comp=$1 si=$2
    local label="${SHAPE}-${TRACE_SUFFIX}-si${si}-${comp}"
    local dir="$OUTROOT/$label"

    # Skip a row that already completed, so an interrupted sweep resumes
    # instead of redoing hours of work.
    if grep -q "End Simulation Statistics" "$dir/stats.txt" 2>/dev/null; then
        echo "=== $label (already done)"
        return 0
    fi

    # cwd and options are omitted rather than passed empty: a workload with no
    # argv would otherwise be handed an empty argument, which is not the same
    # as none.
    local extra=()
    [ -n "$BENCH_CWD" ]  && extra+=(--cwd "$BENCH_CWD")
    [ -n "$BENCH_OPTS" ] && extra+=(--options "$BENCH_OPTS")

    echo "=== $label"
    "$GEM5" --outdir="$dir" "$CFG" \
        --cmd "$BIN" "${extra[@]}" \
        --compression "$comp" \
        --trace-file "$TRACE_FILE" \
        --nvm-to-capacitor \
        --sample-interval "$si" \
        > "$dir.log" 2>&1 || {
            echo "    FAILED -- see $dir.log" >&2
            return 0
        }

    local s="$dir/stats.txt"
    if ! grep -q "End Simulation Statistics" "$s" 2>/dev/null; then
        echo "    INCOMPLETE -- see $dir.log" >&2
        return 0
    fi

    echo "$SHAPE,$TRACE_SUFFIX,$si,$comp,\
$(stat_of simTicks "$s"),\
$(stat_of intermittent.numPowerFailures "$s"),\
$(stat_of intermittent.decisionPoints "$s"),\
$(stat_of system.cpu.dcache.compressor.gatedCompressions "$s"),\
$(stat_of system.cpu.dcache.compressor.sampledCompressions "$s"),\
$(stat_of system.cpu.dcache.compressor.compressionsRun "$s"),\
$(stat_of intermittent.compressionEnergy "$s"),\
$(stat_of system.cpu.dcache.overallMissRate::total "$s")" >> "$CSV"
}

# acc first at each N, then kagura, so a partial sweep still yields whole
# comparable pairs rather than a column of orphaned rows.
for si in $INTERVALS; do
    run acc    "$si"
    run kagura "$si"
done

echo
echo "--- Kagura margin vs ACC, by sample_interval (positive = Kagura slower) ---"
awk -F, 'NR>1 && $1=="'"$SHAPE"'" && $2=="'"$TRACE_SUFFIX"'" {
    if ($4=="acc")    a[$3]=$5
    if ($4=="kagura") k[$3]=$5
}
END {
    printf "%-8s %-14s %-14s %-12s %s\n", "N", "acc ticks", "kagura ticks", "margin %", "sampled"
    n = split("'"$INTERVALS"'", order, " ")
    for (i = 1; i <= n; i++) {
        s = order[i]
        if (a[s] != "" && k[s] != "")
            printf "%-8s %-14s %-14s %+.4f\n", s, a[s], k[s], (k[s]-a[s])/a[s]*100
        else
            printf "%-8s %-14s %-14s %s\n", s, (a[s]==""?"-":a[s]), (k[s]==""?"-":k[s]), "incomplete"
    }
}' "$CSV"

echo
echo "Flat across N  -> confound bounded, the margin is real."
echo "Tracks 1/N     -> part of the margin is our sampler, not the paper's ACC."
echo "Full rows in $CSV"
