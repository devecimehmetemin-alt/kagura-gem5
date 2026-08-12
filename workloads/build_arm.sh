#!/usr/bin/env bash
#
# build_arm.sh — cross-compile selected MiBench benchmarks to ARM static ELF
# binaries for gem5 SE-mode runs.
#
# Run on the Linux build VM after installing the cross toolchain:
#     sudo apt install -y gcc-arm-linux-gnueabi
#     bash workloads/build_arm.sh
#
# Output ARM binaries are collected in workloads/bin/ (git-ignored).
#
# How it works: most MiBench Makefiles hardcode the literal command "gcc"
# instead of $(CC), so `make CC=...` does not redirect them. Instead we put a
# tiny "gcc" (and "cc"/"strip") shim first on PATH that forwards to the ARM
# cross-compiler with -static.
#
# Soft-float Thumb-2, to stay close to the paper's ARMv7-M target: the
# gnueabi (soft-float ABI) toolchain plus -march=armv7-a -mthumb makes the
# application code Thumb-2 with no VFP/NEON register file, like an FPU-less
# Cortex-M -- FP arguments travel in integer registers and FP arithmetic is
# library calls, which is also how such MCUs behave. -march is passed
# explicitly because the gnueabi toolchain's default target is older than
# armv7 and would produce Thumb-1. Residual impurity: the toolchain's static
# glibc objects are plain-ARM ARMv5, so the linked binary is not pure
# Thumb-2; gem5's A-profile decoder executes both. The earlier hard-float
# (gnueabihf) build dragged NEON glibc routines in and forced a 32-register
# FP file into every checkpoint (328 B vs 68 B of architectural state) --
# an artifact the paper's M-profile target does not have.

set -u

# LIBC=glibc (default) or LIBC=musl. Static glibc is the reason a MiBench
# kernel links to ~485 KB of text, which at a 1 KB I-cache is most of the
# miss traffic and so most of the energy. musl statically links to a few tens
# of KB and still issues Linux syscalls, so gem5's SE mode needs no change.
# Install a musl cross toolchain (not in apt) with:
#     curl -LO https://musl.cc/arm-linux-musleabi-cross.tgz
#     sudo tar -xf arm-linux-musleabi-cross.tgz -C /opt
#     export PATH=/opt/arm-linux-musleabi-cross/bin:$PATH
LIBC="${LIBC:-glibc}"

if [ "$LIBC" = "musl" ]; then
    CROSS_GCC="arm-linux-musleabi-gcc"
    CROSS_STRIP="arm-linux-musleabi-strip"
else
    CROSS_GCC="arm-linux-gnueabi-gcc"
    CROSS_STRIP="arm-linux-gnueabi-strip"
fi

# -Os and section GC because the target is a 256 B to 4 kB I-cache: code size
# is the dominant energy term here, not instruction count. --gc-sections drops
# every function the link does not reach, which is most of what a static libc
# pulls in.
CROSS_FLAGS="-static -march=armv7-a -mthumb -Os -ffunction-sections -fdata-sections -Wl,--gc-sections"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MIBENCH="$SCRIPT_DIR/mibench"
BIN_DIR="$SCRIPT_DIR/bin"
SHIM_DIR="/tmp/kagura_armshim"
LOG_DIR="/tmp/kagura_armlogs"

# --- sanity check: toolchain present ----------------------------------------
if ! command -v "$CROSS_GCC" >/dev/null 2>&1; then
    echo "ERROR: $CROSS_GCC not found."
    echo "Install it with: sudo apt install -y gcc-arm-linux-gnueabihf"
    exit 1
fi

# --- build the shim ---------------------------------------------------------
mkdir -p "$SHIM_DIR" "$LOG_DIR" "$BIN_DIR"

cat > "$SHIM_DIR/gcc" <<EOF
#!/bin/sh
exec $CROSS_GCC $CROSS_FLAGS "\$@"
EOF
cp "$SHIM_DIR/gcc" "$SHIM_DIR/cc"

cat > "$SHIM_DIR/strip" <<EOF
#!/bin/sh
exec $CROSS_STRIP "\$@"
EOF

chmod +x "$SHIM_DIR/gcc" "$SHIM_DIR/cc" "$SHIM_DIR/strip"
export PATH="$SHIM_DIR:$PATH"

# --- benchmark list ---------------------------------------------------------
# Format: "relative/dir:binary1 binary2 ..."
# To add a benchmark, append its directory and the binary name(s) its Makefile
# produces (grep the Makefile for the `-o <name>` targets).
BENCHMARKS=(
    "automotive/qsort:qsort_small qsort_large"
    "automotive/bitcount:bitcnts"
    "automotive/basicmath:basicmath_small basicmath_large"
    "automotive/susan:susan"
    "network/dijkstra:dijkstra_small dijkstra_large"
    "network/patricia:patricia"
    "security/sha:sha"
    "security/rijndael:rijndael"
    "security/blowfish:bf"
    "telecomm/CRC32:crc"
    "telecomm/FFT:fft"
    "office/stringsearch:search_small search_large"
)

# --- build loop -------------------------------------------------------------
ok=0
fail=0
echo "Cross-compiling MiBench for ARM (static) -> $BIN_DIR"
echo

for entry in "${BENCHMARKS[@]}"; do
    dir="${entry%%:*}"
    bins="${entry#*:}"
    path="$MIBENCH/$dir"
    log="$LOG_DIR/${dir//\//_}.log"

    echo "=== $dir ==="
    if [ ! -d "$path" ]; then
        echo "  SKIP: directory not found ($path)"
        fail=$((fail + 1))
        echo
        continue
    fi

    # make clean (ignore missing clean target), then build; log everything.
    ( cd "$path" && { make clean >/dev/null 2>&1 || true; } && make ) >"$log" 2>&1

    built_any=0
    for b in $bins; do
        f="$path/$b"
        if [ -f "$f" ] && file "$f" | grep -q "ARM"; then
            cp "$f" "$BIN_DIR/"
            echo "  OK:   $b -> bin/$b"
            built_any=1
        else
            echo "  MISS: $b (not produced or not ARM)"
        fi
    done

    if [ "$built_any" -eq 1 ]; then
        ok=$((ok + 1))
    else
        fail=$((fail + 1))
        echo "  compiler output: $log"
    fi
    echo
done

# --- micro benchmarks (single-file, no Makefile) -----------------------------
for src in "$SCRIPT_DIR"/micro/*.c; do
    [ -e "$src" ] || continue
    name="$(basename "${src%.c}")"
    echo "=== micro/$name ==="
    if "$CROSS_GCC" $CROSS_FLAGS -O2 -o "$BIN_DIR/$name" "$src"; then
        echo "  OK:   $name -> bin/$name"
        ok=$((ok + 1))
    else
        echo "  MISS: $name (compile failed)"
        fail=$((fail + 1))
    fi
    echo
done

echo "----------------------------------------------------------------------"
echo "Done. Benchmarks with at least one ARM binary: $ok   failed: $fail"
echo "libc:         $LIBC"
echo "ARM binaries: $BIN_DIR"
echo "Build logs:   $LOG_DIR"
echo
echo "Text size per binary (this is what has to fit in the I-cache):"
for f in "$BIN_DIR"/*; do
    [ -f "$f" ] || continue
    printf "  %-20s %8s B\n" "$(basename "$f")" \
        "$("$CROSS_STRIP" --version >/dev/null 2>&1 && \
           ${CROSS_GCC%-gcc}-size "$f" 2>/dev/null | awk 'NR==2 {print $1}')"
done
