#!/usr/bin/env bash
#
# Incompressible-data equivalence control: is a CompressedTags cache the same
# cache as a plain one when nothing compresses?
#
# WHY THIS EXISTS
#
# Every compressed-vs-uncompressed comparison in this study assumes the two
# organizations differ ONLY by compression. If CompressedTags at 256 B / 2-way
# does not present the same capacity, set mapping or replacement behaviour as a
# plain Cache at 256 B / 2-way, then compression starts every comparison with a
# conflict-miss handicap that has nothing to do with compression, and every
# margin in the study is offset by an unknown constant.
#
# THE CONTROL
#
# Force ACC permanently into Regular Mode, so every fill takes passThrough()
# and is stored full size:
#
#   --gcp-init 0        starts the predictor at Regular Mode (gcp > 0 is false)
#   --sample-interval 0 removes the escape hatch
#
# It can never leave: nothing co-allocates (so no reward can arrive) and no
# block is marked compressed (so no penalty can arrive). The GCP is frozen and
# the compressor is inert. That cache holds only full-size blocks -- exactly
# what a plain Cache holds -- so the two must agree on every access counter.
#
# Compare against --compression none, which is a plain Cache with default tags.
#
# WHAT COUNTS AS PASSING
#
# Miss counts, hit counts, replacements and simTicks all identical. Miss RATE
# alone is not enough: two configurations can share a rate and differ in the
# access count underneath it.
#
# A difference here is the most serious finding available in this codebase --
# it would mean every compressed row carries an organizational offset -- so the
# script prints the raw counters rather than a verdict, and leaves the judgment
# to a human reading them.
#
# USAGE (from the gem5 root)
#
#   bash mEQUIV.sh
#   WORKLOAD=mixfit_stream SHAPE=burst SHAPE_SUFFIX=d912 bash mEQUIV.sh
#
# Leaves sweep-equiv/{plain,forced}/ and prints the comparison.

set -euo pipefail

GEM5=${GEM5:-./build/ARM/gem5.opt}
CFG=${CFG:-configs/kagura/stage_six_sweep.py}

WORKLOAD=${WORKLOAD:-compfit_stream}
SHAPE=${SHAPE:-burst5}
TRACE_SUFFIX=${TRACE_SUFFIX:-d912}

# Same derivation as mSAMP.sh: read the workload off a real matrix row so the
# control is run on the same machine the study was run on, not a restatement
# of it that could drift.
REF=${REF:-sweep-matrix/${WORKLOAD}-${SHAPE}${TRACE_SUFFIX}-acc}
if [ ! -f "$REF/config.ini" ]; then
    echo "ABORT: no reference row at '$REF/config.ini'." >&2
    echo "Row naming is not uniform -- compfit/mixfit rows carry the trace" >&2
    echo "suffix (compfit_stream-burst5d912-acc) but sha rows do not" >&2
    echo "(sha-burst5-acc). List candidates and pass REF explicitly:" >&2
    echo "  ls -d sweep-matrix/*${SHAPE}*" >&2
    echo "  REF=sweep-matrix/<row> bash mEQUIV.sh" >&2
    echo "Any compressed variant works -- only cmd/cwd/trace_file are read." >&2
    exit 1
fi

REF_CMD=$(awk -F= '/^cmd=/ { sub(/^cmd=/, ""); print; exit }' "$REF/config.ini")
BIN=${BIN:-$(echo "$REF_CMD" | awk '{print $1}')}
BENCH_OPTS=${BENCH_OPTS:-$(echo "$REF_CMD" | cut -s -d' ' -f2-)}
BENCH_CWD=${BENCH_CWD:-$(awk -F= '/^cwd=/ { print $2; exit }' "$REF/config.ini")}
TRACE_FILE=${TRACE_FILE:-$(awk -F= '/^trace_file=/ { print $2; exit }' "$REF/config.ini")}

OUTROOT=${OUTROOT:-sweep-equiv}
mkdir -p "$OUTROOT"

[ -f "$TRACE_FILE" ] || { echo "ABORT: trace '$TRACE_FILE' not found" >&2; exit 1; }
[ -x "$GEM5" ]       || { echo "ABORT: '$GEM5' not built" >&2; exit 1; }
# A missing knob produces a completed run that silently tests nothing -- the
# same failure mode as the sample-interval sweep. Check both sides.
grep -q "gcp-init" "$CFG" || {
    echo "ABORT: $CFG has no --gcp-init flag (config edit not applied)" >&2; exit 1; }
grep -q "gcp_init" build/ARM/params/ACC.hh 2>/dev/null || {
    echo "ABORT: build/ARM/params/ACC.hh has no gcp_init (rebuild gem5)" >&2; exit 1; }

echo "reference row : $REF"
echo "binary        : $BIN ${BENCH_OPTS:-(no argv)}"
echo "trace         : $TRACE_FILE"
echo

run () {
    local label=$1; shift
    local dir="$OUTROOT/$label"

    if grep -q "End Simulation Statistics" "$dir/stats.txt" 2>/dev/null; then
        echo "=== $label (already done)"
        return 0
    fi

    local base=(--cmd "$BIN")
    [ -n "$BENCH_CWD" ]  && base+=(--cwd "$BENCH_CWD")
    [ -n "$BENCH_OPTS" ] && base+=(--options "$BENCH_OPTS")

    echo "=== $label"
    "$GEM5" --outdir="$dir" "$CFG" "${base[@]}" \
        --trace-file "$TRACE_FILE" --nvm-to-capacitor "$@" \
        > "$dir.log" 2>&1 || { echo "    FAILED -- see $dir.log" >&2; return 0; }

    grep -q "End Simulation Statistics" "$dir/stats.txt" 2>/dev/null || \
        echo "    INCOMPLETE -- see $dir.log" >&2
}

# Plain Cache, gem5 default tags, no compressor at all.
run plain  --compression none

# CompressedTags + ACC frozen in Regular Mode: every fill full size.
run forced --compression acc --gcp-init 0 --sample-interval 0

echo
echo "--- D-cache access counters: plain Cache vs CompressedTags with nothing compressed ---"
for s in system.cpu.dcache.overallAccesses::total \
         system.cpu.dcache.overallHits::total \
         system.cpu.dcache.overallMisses::total \
         system.cpu.dcache.overallMissRate::total \
         system.cpu.dcache.replacements \
         system.cpu.icache.overallMisses::total \
         simTicks \
         intermittent.numPowerFailures; do
    p=$(awk -v n="$s" '$1==n {print $2; exit}' "$OUTROOT/plain/stats.txt"  2>/dev/null)
    f=$(awk -v n="$s" '$1==n {print $2; exit}' "$OUTROOT/forced/stats.txt" 2>/dev/null)
    if [ "$p" = "$f" ] && [ -n "$p" ]; then mark="same"; else mark="** DIFFERS **"; fi
    printf "%-42s %18s %18s  %s\n" "${s#system.cpu.}" "${p:--}" "${f:--}" "$mark"
done

echo
echo "--- sanity: the forced row really did compress nothing ---"
for s in system.cpu.dcache.compressor.compressionsRun \
         system.cpu.dcache.compressor.gatedCompressions \
         system.cpu.dcache.compressor.sampledCompressions \
         system.cpu.dcache.compressor.modeSwitches; do
    printf "%-52s %s\n" "${s#system.cpu.dcache.}" \
        "$(awk -v n="$s" '$1==n {print $2; exit}' "$OUTROOT/forced/stats.txt" 2>/dev/null)"
done
echo
echo "compressionsRun must be 0. If it is not, the freeze did not hold and the"
echo "comparison above is between two different caches for the wrong reason."
