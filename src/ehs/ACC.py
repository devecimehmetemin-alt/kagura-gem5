from m5.objects.EnergyCompressor import EnergyCompressor
from m5.params import *


class ACC(EnergyCompressor):
    """Adaptive Cache Compression (Alameldeen & Wood, ISCA 2004).

    One saturating counter of net cycles saved by compression gates
    the wrapped compressor. Fills compress while the counter is positive
    (Compression Mode) and store uncompressed while it is not (Regular Mode).
    A gated fill never runs the compressor, so Regular Mode also stops
    paying compression energy. The counter is fed by an
    ACCCache, which classifies hits.

    The GCP is architectural state (it survives power failures), so its size
    belongs in the controller's ckpt_reg_bytes.
    """

    type = "ACC"
    cxx_class = "gem5::compression::ACC"
    cxx_header = "ehs/acc.hh"

    # 16-bit signed counter
    gcp_min = Param.Int(-32768, "GCP saturation floor")
    gcp_max = Param.Int(32767, "GCP saturation ceiling")

    # Start in Compression Mode with modest confidence.
    # high enough that a few early unlucky decompressions do not
    # flip the mode before any evidence accumulates, low enough
    # that a genuinely useless compressor is switched off early in the run.
    gcp_init = Param.Int(1024, "Initial GCP value")

    # In Regular Mode nothing coallocates, so no reward could ever arriv.
    # Compressing every Nth fill anyway keeps evidence flowing at 1/N of
    # the energy. 0 disables sampling (Regular Mode then becomes permanent).
    sample_interval = Param.Unsigned(64,
        "Regular Mode fills between forced compressions (0 = never)")

    # The GCP is fed only by hits on blocks that compressed. A co-allocated hit
    # rewards it, a hit on a compressed block that earned nothing debits the
    # decompression latency. A compression that FAILS produces a full-size
    # block, which can never co-allocate and is never marked compressed, so it
    # generates neither signal for the rest of its life. Also it costs
    # latency and energy and tells the predictor nothing.
    #
    # Charging a failed attempt closes this loop using information the
    # compressor already produced. 0 disables it, which is the default: every
    # result taken before this parameter existed must reproduce byte for byte.
    failed_penalty = Param.Unsigned(0,
        "GCP cycles debited when a compression attempt comes back full size "
        "(0 = disabled, the published behaviour)")
