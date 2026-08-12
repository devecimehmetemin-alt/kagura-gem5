import m5
from m5.objects import *
import argparse
import os
import shlex
from m5.objects import (
    BDI,
    AddrRange,
    Cache,
    CompressedTags,
    DDR3_1600_8x8,
    EnergyCompressor,
    LRURP,
    MemCtrl,
    Process,
    Root,
    SEWorkload,
    SrcClockDomain,
    System,
    SystemXBar,
    VoltageDomain,
)

# Stage 3 config

# Table I parameters
CLOCK = "200MHz"          # in-order core clock
MEM_SIZE = "16MiB"        # ReRAM main memory
CACHELINE = 32            # 32 B block size (bytes)
L1_SIZE = "256B"          # 256 B I-cache and D-cache
L1_ASSOC = 2              # 2-way


_THIS_DIR = os.path.dirname(os.path.realpath(__file__))

_DEFAULT_BINARY = os.path.normpath(
    os.path.join(_THIS_DIR, "../../tests/test-progs/hello/bin/arm/linux/hello")
)


def _parse_args():
    parser = argparse.ArgumentParser(
        description="Stage 3 run: intermittent power + Table I ReRAM "
        "(MinorCPU + SRAM L1s + checkpoint-cost accounting).",
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
    parser.add_argument(
        "--compression",
        choices=["none", "bdi"],
        default="bdi",
        help="L1 cache compression: 'bdi' (default) fits the L1s with the BDI "
        "compressor and compression-aware tags; 'none' gives the plain "
        "uncompressed cache, i.e. the reference the compressed runs are "
        "measured against. Everything else is held fixed between the two.",
    )
    return parser.parse_args()


args = _parse_args()

# The binary and working directory become absolute so the run does not depend on
# where gem5 was launched from
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

# CPU
system.cpu = IntermittentMinorCPU()


# Split L1 I/D SRAM caches (Table I: 256 B, 2-way, 32 B block, LRU, write-back,
# 1-cycle hit).
_L1 = dict(
    size=L1_SIZE,
    assoc=L1_ASSOC,
    tag_latency=1,
    data_latency=1,
    response_latency=1,
    mshrs=4,
    tgts_per_mshr=20,
    replacement_policy=LRURP(),
)

# Compression energy (Table I)
E_COMPRESS = 3.84e-12     # J per compression   (Table I)
E_DECOMPRESS = 0.65e-12   # J per decompression (Table I)


# Each cache needs its OWN tag store and compressor instance. A compressor is
# bound to a single cache (BaseCache::setCache is one-shot), so the two L1s
# cannot share one SimObject.
def _l1_cache():
    compression = (
        dict(
            tags=CompressedTags(),
            compressor=EnergyCompressor(
                compressor=BDI(),
                e_compress=E_COMPRESS,
                e_decompress=E_DECOMPRESS,
            ),
        )
        if args.compression == "bdi"
        else {}
    )
    return Cache(**_L1, **compression)


system.cpu.icache = _l1_cache()
system.cpu.dcache = _l1_cache()

# The compressors the capacitor pays for. Both L1s when compression is on,
# nothing when it is off.
_COMPRESSORS = (
    [system.cpu.icache.compressor, system.cpu.dcache.compressor]
    if args.compression == "bdi"
    else []
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


# Non-volatile main memory: 16 MB ReRAM with Table I's timings. Table I's
# parameter names (tCK/tBURST/tRCD/tCL/tWTR/tWR/tXAW) are gem5 DRAMInterface
# parameters. NVMInterface has none of them, so the paper modeled ReRAM as
# a DRAM interface with slowed timings, and this does the same. The read/write
# asymmetry ReRAM is known for is carried by tCL (7.5 ns) vs tWR (150 ns).
class ReRAM(DDR3_1600_8x8):
    # Table I: tCK/tBURST/tRCD/tCL/tWTR/tWR/tXAW = 0.94/7.5/18/7.5/15/150/30 ns
    tCK = "0.94ns"
    tBURST = "7.5ns"
    tRCD = "18ns"
    tCL = "7.5ns"
    tWTR = "15ns"
    tWR = "150ns"
    tXAW = "30ns"

    # No refresh in ReRAM: push refresh intervals out beyond any run length.
    tREFI = "10s"

    # 8 devices x 1 MiB x 2 ranks = 16 MiB, matching MEM_SIZE so gem5 does not
    # warn about a capacity mismatch with the address range.
    device_size = "1MiB"


system.mem_ctrl = MemCtrl()
system.mem_ctrl.dram = ReRAM()
system.mem_ctrl.dram.range = system.mem_ranges[0]
system.mem_ctrl.port = system.membus.mem_side_ports

# Back-door port the simulator uses to load the binary into memory.
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

# JIT checkpoint cost.
CKPT_E_PER_BYTE = 10e-12          # J/byte (placeholder, see above)
CKPT_T_PER_BYTE = 150e-9 / 32     # s/byte, from Table I tWR

# Registers on a 32 bit ARM
CKPT_REG_BYTES = 68

root.intermittent = IntermittentController(cpu=system.cpu, capacitance=4.7e-6, v_max=3.0,
                                            v_on=2.4, v_off=1.8, p_harvest=5e-3, harvest_period=10e-3,
                                            duty_cycle=0.5, p_static=0.5e-3 , e_per_inst=80e-12,
                                            ckpt_tags=[system.cpu.dcache.tags],
                                            e_per_byte_nvm=CKPT_E_PER_BYTE,
                                            t_per_byte_nvm=CKPT_T_PER_BYTE,
                                            ckpt_reg_bytes=CKPT_REG_BYTES,
                                            compressors=_COMPRESSORS)
root.intermittent.clk_domain = SrcClockDomain(clock="1GHz", voltage_domain=VoltageDomain())

m5.instantiate()

# Power-cycle loop. When the capacitor reaches v_off the controller exits the
# simulation loop with cause "power failure". At that point the system is
# drained (all in-flight memory accesses complete for JIT
# checkpoint), dirty SRAM lines are written back to NVM (the checkpoint
# itself), the volatile caches are invalidated (SRAM contents are lost while
# off), and the CPU is suspended. The next m5.simulate() resumes the event
# loop with the core dark so the capacitor can recharge; the controller
# reactivates the CPU when the voltage recovers to v_on.
#
# powerOff() runs before the writeback. It prices the checkpoint by
# counting dirty blocks, and memWriteback() clears each dirty bit as it saves
# the block, so counting afterwards would always find nothing to checkpoint.
# CPU registers are preserved by the simulator rather than saved and restored
# explicitly, so they are charged as a flat ckpt_reg_bytes instead.
power_failures = 0
while True:
    exit_event = m5.simulate()
    cause = exit_event.getCause()
    if cause == "power failure":
        power_failures += 1
        m5.drain()
        root.intermittent.powerOff()
        m5.memWriteback(root)
        m5.memInvalidate(root)
    else:
        break

print(f"Exited simulation because {exit_event.getCause()}")
print(f"Power failures survived: {power_failures}")