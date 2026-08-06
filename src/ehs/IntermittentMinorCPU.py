# Derives from the ARM-configured MinorCPU (this project's builds are
# ARM-only), so the subclass keeps the ArmMMU/ISA setup for free.
from m5.objects.ArmCPU import ArmMinorCPU


class IntermittentMinorCPU(ArmMinorCPU):
    """MinorCPU that stays suspended across the drain/resume power cycles
    used for JIT checkpointing (stock MinorCPU wakes suspended threads on
    drainResume)."""

    type = "IntermittentMinorCPU"
    cxx_header = "ehs/intermittent_minor_cpu.hh"
    cxx_class = "gem5::IntermittentMinorCPU"
