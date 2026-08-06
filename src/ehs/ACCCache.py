from m5.objects.Cache import Cache
from m5.params import *


class ACCCache(Cache):
    """A cache that feeds ACC's Global Compression Predictor.

    Classifies every hit to a compressed block, co-allocated means
    compression's extra capacity avoided a miss,
    alone in its superblock means decompression latency was paid for nothing.
    These are what the ACC uses.
    """

    type = "ACCCache"
    cxx_class = "gem5::ACCCache"
    cxx_header = "ehs/acc_cache.hh"

    # No default: this is the miss penalty of the machine the cache sits in,
    # and it should be measured.
    #
    # NEGATIVE means measure it at run time, which is what a sweep needs. A
    # fixed value is the miss penalty of one geometry running one workload; it
    # depends on the queueing the miss rate itself produces, so a constant
    # calibrated at one point misprices every other point, and two swept rows
    # would then differ by their calibration rather than by the policy under
    # test. It is also the closer reading of Alameldeen & Wood, whose predictor
    # follows the latency the machine actually observes.
    reward_cycles = Param.Int(
        "GCP credit per hit that compression made possible (cycles); "
        "negative measures the miss penalty at run time instead")

    # Kagura tunes its compression disabling threshold by AIMD on
    # eviction pressure, which only the cache can see. Set on the D-cache
    # (the paper's architecture is D-cache-centric, Fig. 7), leave NULL when
    # no Kagura controller is in the system.
    kagura = Param.KaguraController(NULL,
        "Kagura controller to report evictions to (optional)")
