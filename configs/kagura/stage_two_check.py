import m5
from m5.objects import *
import argparse
import os
import shlex
from m5.objects import (
    AddrRange,
    Cache,
    LRURP,
    MemCtrl,
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

# CPU
system.cpu = IntermittentMinorCPU()


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

# Non-volatile main memory (16 MB ReRAM)
system.mem_ctrl = MemCtrl()
system.mem_ctrl.dram = NVM_2400_1x64()
system.mem_ctrl.dram.range = system.mem_ranges[0]
system.mem_ctrl.port = system.membus.mem_side_ports

# Back-door port the simulator uses to load the binary into memory.
system.system_port = system.membus.cpu_side_ports

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
CKPT_E_PER_BYTE = 10e-12          # J/byte
CKPT_T_PER_BYTE = 150e-9 / 32     # s/byte, from Table I

# Registers on a 32 bit ARM
CKPT_REG_BYTES = 328

root.intermittent = IntermittentController(cpu=system.cpu, capacitance=4.7e-6, v_max=3.0,
                                            v_on=2.4, v_off=1.8, p_harvest=5e-3, harvest_period=10e-3,
                                            duty_cycle=0.5, p_static=0.5e-3 , e_per_inst=80e-12,
                                            ckpt_tags=[system.cpu.dcache.tags],
                                            e_per_byte_nvm=CKPT_E_PER_BYTE,
                                            t_per_byte_nvm=CKPT_T_PER_BYTE,
                                            ckpt_reg_bytes=CKPT_REG_BYTES)
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