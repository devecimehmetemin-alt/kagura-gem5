from m5.params import *
from m5.util.pybind import PyBindMethod
from m5.objects.ClockedObject import ClockedObject

class IntermittentController(ClockedObject):
    type = "IntermittentController"
    cxx_header = "ehs/intermittent_controller.hh"
    cxx_class = "gem5::IntermittentController"

    # Called from the config's power-cycle loop, after m5.drain(), to
    # suspend the CPU at a clean boundary.
    cxx_exports = [PyBindMethod("powerOff")]
    capacitance = Param.Float("Capacitor size in Farads")
    v_max = Param.Float("Fully charged voltage")
    v_on  = Param.Float("Voltage to resume execution")
    v_off = Param.Float("Voltage that triggers power failure")
    cpu = Param.BaseCPU("CPU gated by capacitor")

    # Default synthetic harvest source
    p_harvest = Param.Float("Harvest power while ON (W)")
    harvest_period = Param.Float("Period of the harvest square wave (s) ")
    duty_cycle = Param.Float("Fraction of each period that is ON (0-1)")

    # File-based harvest trace
    trace_file = Param.String("",
        "Harvest power trace ('time_s power_W' per line; empty: square wave)")
    trace_scale = Param.Float(1.0,
        "Multiplier on trace powers (calibrates a recording to this "
        "machine's power budget)")
    trace_loop = Param.Bool(True,
        "Wrap around when simulated time outruns the trace (False: hold the "
        "final sample)")

    #CPU power consumption modelling
    p_static   = Param.Float("Static/leakage power while powered (W)")
    e_per_inst = Param.Float("Dynamic energy per committed instruction (J)")

    # JIT checkpoint cost. The checkpoint saves every dirty SRAM
    # block plus the architectural state.
    ckpt_tags = VectorParam.BaseTags(
        "Tag stores whose dirty blocks are saved by a checkpoint")
    e_per_byte_nvm = Param.Float("NVM write energy per checkpointed byte (J)")
    t_per_byte_nvm = Param.Float(0.0,
        "NVM write time per checkpointed byte (s); a stat only unless "
        "ckpt_time is set")
    ckpt_reg_bytes = Param.Unsigned(200,
        "Architectural state (registers) saved per checkpoint (B)")

    # The checkpoint reaches NVM functionally (m5.memWriteback, called from the
    # config's power-cycle loop between m5.simulate() calls), so no simulated
    # time passes while it happens: t_per_byte_nvm has always been a statistic.
    # Setting this holds the restore back until the checkpoint's write time
    # has elapsed, which is what physically prevents a machine from resuming
    # before its state is safe.
    ckpt_time = Param.Bool(False,
        "Charge the checkpoint's write time as simulated time (delay the "
        "restore), not just as a statistic")

    # Checkpoint-compression gate. A measurement-only compressor run over the
    # dirty blocks at each checkpoint. Records what the dirty set would have
    # compressed to
    ckpt_probe_compressor = Param.BaseCacheCompressor(NULL,
        "Measurement-only compressor probed over dirty blocks at checkpoint "
        "(NULL: probe disabled)")

    # Golden model vector dump for a later check if required. When set,
    # every dirty block the probe compresses is written out as raw bytes plus
    # the size the probe measured.
    vector_dump_path = Param.String("",
        "Path to dump per-dirty-block compression vectors for RTL "
        "verification (empty: no dump)")

    # Compression energy
    compressors = VectorParam.EnergyCompressor([],
        "Compressors whose energy is drawn from the capacitor")

    # Main-memory (NVM) traffic energy
    nvm = Param.NvmMemCtrl(NULL,
        "Memory controller whose traffic is priced (NULL: not modelled)")

    # ReRAM is read/write asymmetric
    e_per_byte_nvm_read = Param.Float(2e-12,
        "NVM read energy per byte on a cache fill (J)")
    e_per_byte_nvm_write = Param.Float(10e-12,
        "NVM write energy per byte on a dirty writeback (J)")

    # False: account the energy, but do not drain the capacitor with it
    #
    # True:: memory traffic drains the capacitor, so power cycles
    # shorten and the failure count rises.
    nvm_energy_to_capacitor = Param.Bool(False,
        "Drain NVM traffic energy from the capacitor, not just account it")
