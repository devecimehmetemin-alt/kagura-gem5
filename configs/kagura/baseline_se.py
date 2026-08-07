"""
Stage 1 baseline for the intermittence-aware cache compression study.

Models the plain (uncompressed) starting point the later work builds on, matching
the "No Compressor" column of Table I in the paper:

    MinorCPU (in-order) --+-- L1 I-cache (SRAM) --+
                          +-- L1 D-cache (SRAM) --+-- membus -- MemCtrl[NVM]

There is no cache compression, no GCP gating, and no power failure yet; this
config only needs to run a syscall-emulation (SE) workload to completion so the
CPU + cache + NVM path can be sanity-checked before any intermittence machinery
is added.

Most MiBench kernels read their input from a file argument and/or write results
to stdout via shell redirection (e.g. "sha input_small.asc > output.txt"), and a
few use paths relative to their own directory (CRC32's "../adpcm/data/large.pcm").
The options below cover all three: --options for argv, --input/--output/--errout
for stdin/stdout/stderr redirection, and --cwd for the process working directory
that relative input paths resolve against.
"""

import argparse
import os
import shlex

import m5
from m5.objects import (
    AddrRange,
    Cache,
    LRURP,
    MemCtrl,
    MinorCPU,
    NVM_2400_1x64,
    Process,
    Root,
    SEWorkload,
    SrcClockDomain,
    System,
    SystemXBar,
    VoltageDomain,
)


# Table I parameters
CLOCK = "200MHz"          # in-order core clock
MEM_SIZE = "16MiB"        # ReRAM main memory
CACHELINE = 32            # 32 B block size (bytes)
L1_SIZE = "256B"          # 256 B I-cache and D-cache
L1_ASSOC = 2              # 2-way

_THIS_DIR = os.path.dirname(os.path.realpath(__file__))

# Default workload: the ARM "hello" binary shipped with gem5, used for the
# Stage 1 sanity check when no --cmd is given.
_DEFAULT_BINARY = os.path.normpath(
    os.path.join(_THIS_DIR, "../../tests/test-progs/hello/bin/arm/linux/hello")
)


def _parse_args():
    parser = argparse.ArgumentParser(
        description="Stage 1 baseline SE run (MinorCPU + SRAM L1s + NVM).",
    )
    parser.add_argument(
        "--cmd",
        default=_DEFAULT_BINARY,
        help="Path to the ARM SE workload binary "
        "(default: the gem5 ARM hello-world).",
    )
    parser.add_argument(
        "--options",
        default="",
        help='Command-line arguments for the workload, as one quoted string '
        '(e.g. --options "input_small.dat"). Split on whitespace.',
    )
    parser.add_argument(
        "--input",
        default="",
        help="File to redirect the workload's stdin from.",
    )
    parser.add_argument(
        "--output",
        default="",
        help="File to redirect the workload's stdout to.",
    )
    parser.add_argument(
        "--errout",
        default="",
        help="File to redirect the workload's stderr to.",
    )
    parser.add_argument(
        "--cwd",
        default="",
        help="Working directory for the process. Relative input-file "
        "arguments (and --input/--output/--errout) resolve against it.",
    )
    return parser.parse_args()


args = _parse_args()

BINARY = os.path.abspath(args.cmd)
CWD = os.path.abspath(args.cwd) if args.cwd else None


def _resolve(path):
    """Absolutise a redirection path against --cwd (or the launch dir)."""
    if not path:
        return ""
    if os.path.isabs(path):
        return path
    return os.path.abspath(os.path.join(CWD, path) if CWD else path)

#initialize system
system = System()

system.clk_domain = SrcClockDomain()
system.clk_domain.clock = CLOCK
system.clk_domain.voltage_domain = VoltageDomain()

system.mem_mode = "timing"
system.mem_ranges = [AddrRange(MEM_SIZE)]
system.cache_line_size = CACHELINE

#CPU
system.cpu = MinorCPU()

# Split L1 I/D SRAM caches (Table I: 256 B, 2-way, 32 B block, LRU, write-back,
# 1-cycle hit)

system.cpu.icache = Cache(
    size=L1_SIZE,
    assoc=L1_ASSOC,
    tag_latency=1,
    data_latency=1,
    response_latency=1,
    mshrs=4,
    tgts_per_mshr=20,
    replacement_policy=LRURP(),
)
system.cpu.dcache = Cache(
    size=L1_SIZE,
    assoc=L1_ASSOC,
    tag_latency=1,
    data_latency=1,
    response_latency=1,
    mshrs=4,
    tgts_per_mshr=20,
    replacement_policy=LRURP(),
)


# Memory bus and connections
system.membus = SystemXBar()

# CPU request ports -> cache cpu_side ports
system.cpu.icache.cpu_side = system.cpu.icache_port
system.cpu.dcache.cpu_side = system.cpu.dcache_port

# Cache mem_side ports -> bus cpu_side_ports
system.cpu.icache.mem_side = system.membus.cpu_side_ports
system.cpu.dcache.mem_side = system.membus.cpu_side_ports

system.cpu.createInterruptController()

# TODO: retune NVM timings to Table I's ReRAM values
#       (tCK/tBURST/tRCD/tCL/tWTR/tWR/tXAW = 0.94/7.5/18.0/15.0/7.5/150/30 ns);
#       defaults are used for the Stage 1 sanity check.
system.mem_ctrl = MemCtrl()
system.mem_ctrl.dram = NVM_2400_1x64()
system.mem_ctrl.dram.range = system.mem_ranges[0]
system.mem_ctrl.port = system.membus.mem_side_ports

# Port the simulator uses to load the binary into memory.
system.system_port = system.membus.cpu_side_ports

# Workload (SE mode)
if not os.path.isfile(BINARY):
    m5.fatal(f"Workload binary not found: {BINARY}")

system.workload = SEWorkload.init_compatible(BINARY)

process = Process()
# argv[0] is the binary; the rest are the benchmark's own arguments.
process.cmd = [BINARY] + shlex.split(args.options)
if CWD is not None:
    process.cwd = CWD
if args.input:
    process.input = _resolve(args.input)
if args.output:
    process.output = _resolve(args.output)
if args.errout:
    process.errout = _resolve(args.errout)

system.cpu.workload = process
system.cpu.createThreads()

# Instantiate and run
root = Root(full_system=False, system=system)
m5.instantiate()

print("Beginning simulation!")
exit_event = m5.simulate()
print(f"Exiting @ tick {m5.curTick()} because {exit_event.getCause()}")
