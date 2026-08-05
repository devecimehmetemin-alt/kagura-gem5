from m5.objects.IntermittentController import IntermittentController
from m5.params import *


class KaguraController(IntermittentController):

    type = "KaguraController"
    cxx_header = "ehs/kagura_controller.hh"
    cxx_class = "gem5::KaguraController"

    # The paper's own example value
    n_thres_init = Param.Unsigned(8,
        "Initial compression-disabling threshold (N_thres)")

    # The paper rewards the 2-bit counter when the estimate is close
    closeness_frac = Param.Float(0.1,
        "Estimate error, as a fraction of R_prev, still rewarded as close")

    sat_counter_init = Param.Unsigned(1,
        "Initial value of the 2-bit estimate-confidence counter")

    # AIMD tuning of N_thres is the paper's design and stays on for
    # default Kagura
    aimd = Param.Bool(True,
        "Self-tune N_thres by AIMD on eviction pressure; if false, N_thres "
        "stays pinned at n_thres_init")

    # VETO design
    thres_cap_frac = Param.Float(0.0,
        "Bound R_thres to this fraction of R_prev at each reboot "
        "(0 disables; only meaningful with aimd)")

    # VETO design
    rm_confidence_min = Param.Unsigned(0,
        "Minimum 2-bit confidence counter value required to enter Regular "
        "Mode (0 = published rule, no gate)")

    # perceptron design
    rm_perceptron = Param.Bool(False,
        "Gate Regular Mode entry with a perceptron over cycle-outcome "
        "history instead of the 2-bit counter floor")

    perceptron_history = Param.Unsigned(8,
        "Cycle outcomes remembered (H); weights are H+1 with the bias")

    perceptron_volatile = Param.Bool(False,
        "Zero the perceptron at each reboot (weights die with the power "
        "failure instead of being checkpointed)")

    perceptron_theta = Param.Int(0,
        "Training threshold; 0 = Jimenez fitted value 1.93*H + 14")
