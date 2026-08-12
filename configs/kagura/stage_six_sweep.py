import m5
from m5.objects import *
import argparse
import os
import shlex

# Explicit submodule import: ArmCPU.py is registered with sim_objects=[], so
# its classes are not guaranteed to come through the star import above.
from m5.objects.ArmCPU import ArmTimingSimpleCPU

from m5.objects import (
    ACC,
    ACCCache,
    BDI,
    AddrRange,
    Cache,
    CompressedTags,
    DDR3_1600_8x8,
    EnergyCompressor,
    IntermittentController,
    KaguraController,
    LRURP,
    MemCtrl,
    NvmMemCtrl,
    Process,
    Root,
    SEWorkload,
    SrcClockDomain,
    System,
    SystemXBar,
    VoltageDomain,
)

# Stage 6 sweep. Stage 5's system with the parameters opened up as knobs.
# Kagura cut compression energy 16.30 -> 15.52 uJ there (-4.8%), and two
# things about that number need checking:
#
#  1. AIMD saturated. R_evict ran in the thousands against a single-digit
#     R_thres, so R_evict > R_thres/2 held at every decision: all 40 halved,
#     none grew. The 4.8% is a floor set by the control loop, not by the
#     mechanism. --no-aimd + --n-thres pins the threshold so the curve can be
#     traced directly.
#
#  2. The saving was all I-cache. ACC had already gated the D-cache off, so
#     Kagura's D-cache gating bought nothing. Is the benefit intermittence-
#     awareness, or a repair to a predictor blind to a read-only cache?
#     --icache-compression off answers it: if never compressing the I-cache
#     saves the same energy, the benefit is not Kagura's to claim.
#
# --l1-size checks whether either survives at a realistic size. Checkpoint
# energy grows with the dirty set, compression energy falls with the fill
# count, so they cross somewhere above 256 B.

# Table I parameters. All knobs now, so a bare run is the Stage 5 point.
CLOCK = "200MHz"          # in-order core clock
MEM_SIZE = "16MiB"        # ReRAM main memory
CACHELINE = 32            # 32 B block size (bytes)
L1_SIZE = "256B"          # 256 B I-cache and D-cache
L1_ASSOC = 2              # 2-way
CAPACITANCE = 4.7e-6      # 4.7 uF energy buffer (Table I)

# Capacitor voltage window. Not in Table I, which gives only the capacitance.
# These are the values stages 2-5 were verified at; the same group's HPCA'25
# paper uses 3.5/2.8, which delivers 1.75x the energy per power cycle.
V_MAX = 3.0
V_ON = 2.4
V_OFF = 1.8

_THIS_DIR = os.path.dirname(os.path.realpath(__file__))

_DEFAULT_BINARY = os.path.normpath(
    os.path.join(_THIS_DIR, "../../tests/test-progs/hello/bin/arm/linux/hello")
)


def _parse_args():
    parser = argparse.ArgumentParser(
        description="Stage 6 sweep: Stage 5's system (intermittent power + "
        "Table I ReRAM + Kagura over ACC-gated BDI) with the cache geometry, "
        "the capacitor, and Kagura's threshold policy exposed as knobs.",
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
        choices=["none", "bdi", "acc", "kagura"],
        default="kagura",
        help="L1 cache compression: 'kagura' (default) is ACC-gated BDI with "
        "Kagura's end-of-cycle mode switching on top; 'acc' is the stage 4 "
        "result Kagura must beat; 'bdi' is the compressor always on; 'none' "
        "is the plain uncompressed cache.",
    )

    core = parser.add_argument_group("core model")
    core.add_argument(
        "--cpu-type",
        choices=["minor", "timing"],
        default="minor",
        help="Core model (default: minor, kept so earlier results stay "
        "reproducible). 'minor' is gem5's four-stage in-order MinorCPU with "
        "a two-ALU function-unit pool -- an A-class in-order core. 'timing' "
        "is TimingSimpleCPU: one instruction per cycle plus memory stalls, "
        "which matches both the paper's description of the core and the "
        "flat 160 uW/MHz per-cycle power its baseline is priced at. The "
        "McPAT template should describe whichever of these is selected.",
    )

    # Cache geometry
    geom = parser.add_argument_group("cache geometry")
    geom.add_argument(
        "--l1-size",
        default=L1_SIZE,
        help=f"Size of BOTH L1 caches, as a gem5 size string "
        f"(default: {L1_SIZE}, Table I). The sweep axis for the "
        f"compression-versus-checkpoint crossover.",
    )
    geom.add_argument(
        "--l1-assoc",
        type=int,
        default=L1_ASSOC,
        help=f"Associativity of both L1 caches (default: {L1_ASSOC}).",
    )
    geom.add_argument(
        "--cacheline",
        type=int,
        default=CACHELINE,
        help=f"Cache block size in bytes (default: {CACHELINE}). BDI's "
        f"sub-compressors inherit it, so this also sets how many 64-bit "
        f"chunks each compression sees.",
    )
    geom.add_argument(
        "--icache-compression",
        choices=["on", "off"],
        default="on",
        help="'off' leaves the I-cache uncompressed regardless of "
        "--compression, so only the D-cache carries a compressor. This is the "
        "attribution control for the Stage 5 result: Kagura's entire measured "
        "saving came from gating I-cache fills that ACC's latency-driven "
        "predictor can never gate, so 'acc --icache-compression off' is the "
        "trivial baseline that Kagura has to beat before the saving can be "
        "credited to intermittence-awareness rather than to a defect in ACC.",
    )

    # Kagura policy
    kag = parser.add_argument_group("Kagura threshold policy")
    kag.add_argument(
        "--n-thres",
        type=int,
        default=8,
        help="Initial compression-disabling threshold N_thres (default: 8, "
        "the paper's example value). With AIMD on this only sets how fast the "
        "threshold reaches its own range; it is a real variable only under "
        "--no-aimd.",
    )
    kag.add_argument(
        "--no-aimd",
        action="store_true",
        help="Pin N_thres at --n-thres for the whole run instead of "
        "self-tuning it (Sec. VI-B). Not the paper's design: it is the "
        "instrument for measuring the curve AIMD is searching, because AIMD "
        "compares an eviction count against an operation count and at small "
        "cache sizes that comparison saturates.",
    )
    kag.add_argument(
        "--thres-cap",
        type=float,
        default=0.0,
        help="Bound N_thres to this fraction of R_prev at each reboot "
        "(default: 0, off -- the published rule). NOT the paper's design: "
        "the AIMD halving test compares an eviction count against "
        "R_thres/2, so in cycles that produce fewer evictions than that the "
        "threshold can never halve again, compounds past the cycle length, "
        "and Regular Mode swallows the cycle. This is the repair for that "
        "runaway regime, the counterpart of --no-aimd's saturation regime.",
    )
    kag.add_argument(
        "--rm-confidence",
        type=int,
        default=0,
        help="Minimum 2-bit confidence counter value required to enter "
        "Regular Mode (default: 0, off -- the published rule). NOT the "
        "paper's design: the decision fires on R_prev - R_mem <= R_thres, "
        "so a cycle that outruns its predecessor puts its whole overrun "
        "into Regular Mode however small the threshold -- the estimator's "
        "failure mode, which --thres-cap cannot reach (measured: capped "
        "rows tick-identical to the published rule on compfit under the "
        "bursty trace). The counter already scores the estimate but only "
        "gates the R_adjust correction; this floor lets it veto the "
        "decision, so Kagura degrades to plain ACC exactly where its "
        "history is uninformative. 2 = enter only on demonstrated "
        "reliability.",
    )
    kag.add_argument(
        "--sat-init",
        type=int,
        default=1,
        help="Initial value of the 2-bit estimate-confidence counter "
        "(default: 1, the stage 5 choice: apply the R_adjust correction "
        "until raw history proves itself). Matters under alternating cycle "
        "lengths: starting <= 1 lets one bad transition switch the "
        "correction on, whose extrapolation then wrecks the following "
        "matched cycle's estimate, keeping the counter low and the "
        "correction firing -- a self-perpetuating spiral measured on the "
        "AABB harvest (3 rewards vs 16 punishments despite 9 of 10 cycle "
        "pairs matching within 10%%). Starting at 3 keeps the counter in "
        "{2,3} under alternation and the correction off.",
    )
    kag.add_argument(
        "--rm-perceptron",
        action="store_true",
        help="Gate Regular Mode entry with a perceptron over the last "
        "--perc-history cycle outcomes instead of the --rm-confidence "
        "counter floor (mutually exclusive with it). Same training signal "
        "as the 2-bit counter, which keeps running and keeps gating "
        "R_adjust as published -- the entry gate is the only variable. "
        "Tests whether the counter's failure is capacity (a learnable "
        "outcome pattern it cannot hold) or information (no pattern to "
        "learn, in which case the perceptron converges to its bias weight "
        "= a counter).",
    )
    kag.add_argument(
        "--perc-history",
        type=int,
        default=8,
        help="Perceptron outcome-history length H (default: 8; weights are "
        "H+1 including the bias, checkpoint cost H+1+ceil(H/8) bytes)",
    )
    kag.add_argument(
        "--perc-theta",
        type=int,
        default=0,
        help="Perceptron training threshold (default: 0 = the fitted "
        "1.93*H + 14)",
    )
    kag.add_argument(
        "--perc-volatile",
        action="store_true",
        help="Zero the perceptron at each reboot: models the weights "
        "living in SRAM and dying with the power failure instead of being "
        "checkpointed. The predictor never leaves warmup; the run measures "
        "what an unpersisted learner is worth on this device (nothing, "
        "predictably -- but measured).",
    )
    kag.add_argument(
        "--closeness-frac",
        type=float,
        default=0.1,
        help="Estimate error, as a fraction of R_prev, still rewarded as "
        "'close' by the 2-bit confidence counter (default: 0.1). The paper "
        "does not quantify 'close'.",
    )
    kag.add_argument(
        "--dirty-aware",
        action="store_true",
        help="Exempt D-cache fills that serve a write from Kagura's Regular "
        "Mode gate. NOT the paper's design: its late-cycle argument (a block "
        "compressed now dies with the SRAM before paying for itself) is only "
        "true of clean blocks, while a dirty block is written back to NVM by "
        "the checkpoint itself, so compressing it prepays that writeback at "
        "roughly 40:1. The size sweep showed the ungated design self-defeats "
        "through exactly this term (net loss for every cache of 2 kB and up); "
        "this flag is the proposed repair.",
    )

    # Checkpoint-compression gate
    parser.add_argument(
        "--ckpt-gate",
        action="store_true",
        help="Probe every dirty block with a measurement-only BDI at each "
        "checkpoint and report what the dirty set would have compressed to "
        "(ckptProbe* stats). Changes no behaviour and charges no energy: "
        "this is the go/no-go measurement for checkpoint-time compression, "
        "run BEFORE building the mechanism. Requires --compression none -- "
        "under a compressed cache the blocks are already stored compressed "
        "and the probe would double-count, and the gate's question is what "
        "the dirty set of a NORMAL cache looks like.",
    )
    parser.add_argument(
        "--vector-dump",
        default="",
        metavar="PATH",
        help="With --ckpt-gate, also write each probed dirty block to PATH as "
        "'<bytes hex> <size_bits> <comp_bytes>' -- the golden-model corpus for "
        "the hardware BDI testbenches. Empty (default): no dump.",
    )

    # Energy environment
    pwr = parser.add_argument_group("energy environment")
    pwr.add_argument(
        "--capacitance",
        type=float,
        default=CAPACITANCE,
        help=f"Storage capacitor, in farads (default: {CAPACITANCE}). Sets "
        f"the length of a power cycle, and so the size of the population "
        f"Kagura's N_remain estimate is drawn from.",
    )
    pwr.add_argument(
        "--v-max",
        type=float,
        default=V_MAX,
        help=f"Fully-charged capacitor voltage, V (default: {V_MAX}).",
    )
    pwr.add_argument(
        "--v-on",
        type=float,
        default=V_ON,
        help=f"Voltage the core powers up at, V (default: {V_ON}).",
    )
    pwr.add_argument(
        "--v-off",
        type=float,
        default=V_OFF,
        help=f"Voltage the core powers down at, V (default: {V_OFF}). With "
        f"--v-on this sets the usable energy per power cycle, "
        f"0.5*C*(v_on^2 - v_off^2), and so the committed instructions per "
        f"power cycle that Fig. 14 reports. The paper gives the 4.7 uF but "
        f"no voltages; the same group's HPCA'25 paper uses 3.5/2.8, which "
        f"is 1.75x the energy per cycle these defaults deliver.",
    )
    pwr.add_argument(
        "--trace-file",
        type=str,
        default="",
        help="Harvest power trace file ('time_s power_W' per line; '#' "
        "comments). Empty (the default) keeps the 5 mW / 10 ms / 50%% square "
        "wave every earlier result was validated on. The difference is the "
        "regime, not the waveform: under the square wave a death arrives "
        "every period no matter what the program spent (failures = runtime / "
        "10 ms exactly), so saved energy cannot buy cycle length; under a "
        "bursty trace the cycle boundary is energy-limited and saved energy "
        "delays the death -- the regime the paper's speedup channel lives in.",
    )
    pwr.add_argument(
        "--trace-scale",
        type=float,
        default=1.0,
        help="Multiplier applied to trace powers (default: 1.0). The bundled "
        "synthetic trace is pre-calibrated to the square wave's 2.5 mW "
        "average; use this to bring an external recording (e.g. the real "
        "RFHome) onto this machine's power budget.",
    )
    pwr.add_argument(
        "--nvm-to-capacitor",
        action="store_true",
        help="Drain main-memory traffic energy from the capacitor, instead of "
        "only accounting it. NVM traffic is the largest term in this system "
        "(a 256 B run of qsort moves ~215 MB to and from ReRAM), so this is "
        "the physically honest model -- but it is not a more accurate version "
        "of the same experiment. It couples the cache to the power supply: a "
        "bigger cache misses less, draws less power, and runs LONGER on a "
        "charge, a feedback loop the unfed model cannot express and which the "
        "cache-size study cannot be done without. It also shortens every power "
        "cycle, moving the system into the short-cycle regime the paper "
        "actually targets. Every number validated without it will move.",
    )
    pwr.add_argument(
        "--ckpt-time",
        action="store_true",
        help="Charge the checkpoint's NVM write time as simulated time, by "
        "holding the restore back until the write would have finished, "
        "instead of only recording it as checkpointTicks. The checkpoint "
        "reaches NVM functionally (m5.memWriteback, outside m5.simulate()), "
        "so it has always been instantaneous. At tens of power failures that "
        "is negligible -- 17 B of Kagura register state at 4.6875 ns/B is "
        "80 ns per failure, 0.0003% of a 30-failure run. At thousands it is "
        "decisive, because the cost is per FAILURE while the effects it "
        "competes against are per OPERATION: the same term is 0.064% of a "
        "7700-failure run, larger than the whole Kagura-vs-ACC margin there. "
        "Only the write is charged; the restore read stays free, so this is "
        "the conservative half. Off by default -- it re-baselines every "
        "existing result, since each policy pays its own checkpoint size.",
    )
    pwr.add_argument(
        "--e-nvm-read",
        type=float,
        default=2e-12,
        help="NVM read energy per byte, J (default: 2e-12, the original "
        "placeholder, kept so older CSVs stay comparable). Published "
        "anchors: NVSim-derived RRAM figures for intermittent systems come "
        "to 1.3-1.7e-12 J/B (Freezer, arXiv:2101.09968, Table VI: 5.1-6.7 "
        "pJ per 32-bit word), while the measured 40 nm EMBER macro reads at "
        "1.0 pJ/bit = 8e-12 J/B. The placeholder sits inside that range; "
        "every NVM energy result is linear in this knob, so report the "
        "bracket, not one point.",
    )
    pwr.add_argument(
        "--e-nvm-write",
        type=float,
        default=10e-12,
        help="NVM write energy per byte, J (default: 10e-12, the original "
        "placeholder). Published anchors: NVSim-derived RRAM writes are "
        "5.3-7.1e-12 J/B (Freezer, arXiv:2101.09968, Table VI: 21.3-28.6 pJ "
        "per 32-bit word); measured macro writes run 3-10x their read "
        "energy, so the EMBER-grade read anchor implies roughly "
        "24-80e-12 J/B. Linear everywhere, like the read knob.",
    )
    pwr.add_argument(
        "--e-per-inst",
        type=float,
        default=80e-12,
        help="Core dynamic energy per committed instruction, J (default: "
        "80e-12, the stage 2 placeholder, kept so every existing result "
        "stays reproducible). This knob is the denominator of every "
        "relative speedup: core drain sets the total energy per power "
        "cycle, so an inflated value dilutes a fixed compression-energy "
        "saving by the same factor. Paper regime (Table I + Sec. VIII): a "
        "45 nm McPAT-LOP in-order core at 200 MHz, with the only explicit "
        "figure being the 9 pJ SRAM access. Folding SRAM in per "
        "instruction -- one I-fetch plus ~0.3 D-accesses -- and taking "
        "~10 pJ/inst for an LOP-class in-order pipeline gives "
        "~22e-12 J/inst. That is an estimate assembled from Table I and "
        "published LOP figures, not a McPAT run; retune when one lands.",
    )
    pwr.add_argument(
        "--p-static",
        type=float,
        default=0.5e-3,
        help="Static (leakage) power drawn while powered, W (default: "
        "0.5e-3, the stage 2 placeholder). LOP cells exist to cut "
        "leakage: for the paper's 0.538 mm2 core at 45 nm (Sec. VIII-A) "
        "tens of uW is the plausible scale, so the placeholder is likely "
        "an order high. ~50e-6 is the paper-regime estimate; same "
        "provenance caveat as --e-per-inst.",
    )

    # ACC calibration.
    #
    # The reward is the miss penalty compression avoided, which is a property
    # of the machine rather than of the policy: it moves with cache geometry,
    # workload, and the queueing the miss rate itself produces. A constant
    # cannot survive a sweep -- one calibration misprices every other cell, and
    # two rows would then differ by their calibration instead of by the policy.
    # Default -1 measures it at run time and reports it as avgMissPenaltyTicks,
    # so any row can be audited.
    #
    # Stages 3-5 measured it by hand at 256 B on qsort and passed 17/19 in.
    # Those subtracted a 3-cycle hit, which is tag+data+response summed and not
    # what gem5 charges: tag and data go in parallel, so the hit is 1 cycle and
    # response latency belongs to the miss path. The measured path is correct
    # (ACCCache::currentReward); the hand constants stay as they were so the
    # frozen stage 4/5 rows still reproduce.
    acc = parser.add_argument_group("ACC calibration")
    acc.add_argument(
        "--reward-cycles-dcache",
        type=int,
        default=-1,
        help="GCP reward per D-cache hit that compression made possible, in "
        "cycles (default: -1, measure it at run time; 17 is the hand "
        "calibration the stage 4/5 results used, valid only at 256 B on qsort).",
    )
    acc.add_argument(
        "--reward-cycles-icache",
        type=int,
        default=-1,
        help="GCP reward per I-cache hit that compression made possible, in "
        "cycles (default: -1, measure it at run time; 19 is the hand "
        "calibration the stage 4/5 results used, valid only at 256 B on qsort).",
    )
    parser.add_argument(
        "--sw-hint-lo",
        type=lambda v: int(v, 0),
        default=0,
        help="Start of the VIRTUAL address range software marks as "
        "incompressible; fills landing in it bypass the compressor entirely. "
        "This is the hardware-software codesign oracle: a page-table hint "
        "with no lookup cost and no mispredicts, so a run with it set bounds "
        "what any real hint could achieve. Take the bounds from the binary "
        "(nm), since SE-mode requests carry virtual addresses.",
    )
    parser.add_argument(
        "--sw-hint-hi",
        type=lambda v: int(v, 0),
        default=0,
        help="End of that range, exclusive. Equal bounds disable the hint.",
    )
    parser.add_argument(
        "--failed-penalty",
        type=int,
        default=0,
        help="GCP cycles to debit when a compression attempt comes back full "
        "size (default 0 = the published behaviour). ACC's predictor is fed "
        "only by hits on blocks that compressed, so a failed attempt costs "
        "latency and energy while producing no signal at all; on sha 99.90%% "
        "of attempts fail and ACC still gates nothing. This is the "
        "hardware-only alternative to a software compressibility hint.",
    )
    parser.add_argument(
        "--sample-interval",
        type=int,
        default=64,
        help="Regular Mode fills between forced compressions (default 64, "
        "matching ACC.py; 0 disables sampling and makes Regular Mode "
        "permanent). Sampling is NOT part of the published ACC: it exists "
        "because gem5's tags are not decoupled, so a gated fill can never "
        "co-allocate and the predictor would wedge at its floor. Kagura's "
        "gate outranks the sampler, so a Kagura row suppresses these forced "
        "compressions too -- meaning part of any measured Kagura-over-ACC "
        "saving is the removal of an artifact of this recovery mechanism "
        "rather than of the paper's ACC. Sweeping it is how that confound "
        "gets bounded: a margin flat in this knob is not coming from here.",
    )
    parser.add_argument(
        "--gcp-init",
        type=int,
        default=1024,
        help="Initial GCP value (default 1024, matching ACC.py). A value <= 0 "
        "starts ACC in Regular Mode; combined with --sample-interval 0 it can "
        "never leave, because nothing co-allocates to reward it and nothing is "
        "compressed to penalise it. That is the incompressible-data control: "
        "every fill goes to full size, so a CompressedTags cache should then "
        "be organizationally identical to a plain one and any miss-rate "
        "difference is tag geometry rather than compression.",
    )
    return parser.parse_args()


args = _parse_args()

# Every knob below belongs to one policy. Set one the chosen --compression
# cannot act on and it sits there inert while the row claims a policy it never
# ran, so these are fatals rather than warnings. The defaults match ACC.py
# (sample-interval 64, gcp-init 1024), so an unset flag never trips one.
if args.no_aimd and args.compression != "kagura":
    m5.fatal("--no-aimd is a Kagura knob, but --compression is "
             f"'{args.compression}'")

if args.sw_hint_lo != args.sw_hint_hi:
    if args.sw_hint_lo > args.sw_hint_hi:
        m5.fatal("--sw-hint-lo must not exceed --sw-hint-hi "
                 f"(got {args.sw_hint_lo:#x} > {args.sw_hint_hi:#x})")
    if args.compression not in ("acc", "kagura"):
        m5.fatal("--sw-hint-lo/--sw-hint-hi need the ACC compressor, but "
                 f"--compression is '{args.compression}'")
if args.failed_penalty and args.compression not in ("acc", "kagura"):
    m5.fatal("--failed-penalty is an ACC predictor knob, but --compression is "
             f"'{args.compression}'")
if args.sample_interval != 64 and args.compression not in ("acc", "kagura"):
    m5.fatal("--sample-interval is an ACC predictor knob, but --compression "
             f"is '{args.compression}'")
if args.sample_interval < 0:
    m5.fatal("--sample-interval counts fills; 0 disables sampling, negative "
             "is meaningless")
if args.gcp_init != 1024 and args.compression not in ("acc", "kagura"):
    m5.fatal("--gcp-init is an ACC predictor knob, but --compression is "
             f"'{args.compression}'")
if not -32768 <= args.gcp_init <= 32767:
    m5.fatal("--gcp-init must fit the 16-bit signed GCP (-32768..32767)")

if args.thres_cap and args.compression != "kagura":
    m5.fatal("--thres-cap is a Kagura knob, but --compression is "
             f"'{args.compression}'")
if args.thres_cap and args.no_aimd:
    m5.fatal("--thres-cap bounds the AIMD update; with --no-aimd there is "
             "nothing to bound")
if args.rm_confidence and args.compression != "kagura":
    m5.fatal("--rm-confidence is a Kagura knob, but --compression is "
             f"'{args.compression}'")
if not 0 <= args.rm_confidence <= 3:
    m5.fatal("--rm-confidence compares against a 2-bit counter (0..3)")
if args.rm_perceptron and args.compression != "kagura":
    m5.fatal("--rm-perceptron is a Kagura knob, but --compression is "
             f"'{args.compression}'")
if args.rm_perceptron and not 1 <= args.perc_history <= 64:
    m5.fatal("--perc-history must be 1..64")
if not 0 <= args.sat_init <= 3:
    m5.fatal("--sat-init is a 2-bit counter value (0..3)")
if args.ckpt_gate and args.compression != "none":
    m5.fatal("--ckpt-gate probes the dirty set of an UNCOMPRESSED cache; "
             f"--compression is '{args.compression}'")
if args.vector_dump and not args.ckpt_gate:
    m5.fatal("--vector-dump needs --ckpt-gate: the dump is written by the "
             "checkpoint probe, which only runs under the gate")

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
system.cache_line_size = args.cacheline

# CPU
#
# The paper says "single-core in-order five-stage pipeline" and its baseline
# (NVSRAMCache) prices the core at a flat 160 uW/MHz, i.e. a fixed energy per
# cycle with no pipeline detail. MinorCPU is a four-stage model whose default
# function-unit pool is two integer ALUs plus mul/div/FP-SIMD -- an A-class
# in-order core, not a microcontroller, and the reason the McPAT template came
# out A9-shaped. TimingSimpleCPU is the closer match: one instruction per
# cycle plus memory stalls, which is exactly what a flat per-cycle figure
# describes.
#
# TimingSimpleCPU needs no subclass, but it does need the controller to
# suspend through the thread context rather than calling suspendContext() on
# the CPU: its drainResume() re-activates any thread whose status is Active,
# and only ThreadContext::suspend() sets that status. MinorCPU never exposed
# this because IntermittentMinorCPU ignores thread status entirely.
if args.cpu_type == "timing":
    system.cpu = ArmTimingSimpleCPU()
else:
    system.cpu = IntermittentMinorCPU()


# Split L1 I/D SRAM caches (Table I: 256 B, 2-way, 32 B block, LRU, write-back,
# 1-cycle hit -- defaults now rather than constants).
_L1 = dict(
    size=args.l1_size,
    assoc=args.l1_assoc,
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


# Each cache needs its OWN tag store and compressor instance: a compressor is
# bound to a single cache (BaseCache::setCache is one-shot), so the two L1s
# cannot share one SimObject. kagura builds the same caches as acc -- it is a
# layer on top, wired at the controller below.
def _l1_cache(reward_cycles, compress, dirty_aware=False):
    if not compress:
        return Cache(**_L1)
    if args.compression in ("acc", "kagura"):
        return ACCCache(
            **_L1,
            reward_cycles=reward_cycles,
            tags=CompressedTags(),
            compressor=ACC(
                compressor=BDI(),
                e_compress=E_COMPRESS,
                e_decompress=E_DECOMPRESS,
                dirty_aware=dirty_aware,
                sw_hint_lo=args.sw_hint_lo,
                sw_hint_hi=args.sw_hint_hi,
                failed_penalty=args.failed_penalty,
                sample_interval=args.sample_interval,
                gcp_init=args.gcp_init,
            ),
        )
    if args.compression == "bdi":
        return Cache(
            **_L1,
            tags=CompressedTags(),
            compressor=EnergyCompressor(
                compressor=BDI(),
                e_compress=E_COMPRESS,
                e_decompress=E_DECOMPRESS,
            ),
        )
    return Cache(**_L1)


_COMPRESS_D = args.compression != "none"
_COMPRESS_I = _COMPRESS_D and args.icache_compression == "on"

# The dirty exemption is a D-cache policy: instruction fills never serve a
# write, so on the I-cache the flag could never fire.
system.cpu.icache = _l1_cache(args.reward_cycles_icache, _COMPRESS_I)
system.cpu.dcache = _l1_cache(
    args.reward_cycles_dcache, _COMPRESS_D, dirty_aware=args.dirty_aware
)

# The compressors the capacitor pays for. For kagura this is also the mode-
# broadcast path: the controller flips every compressor on it to Regular Mode
# at the decision point, the I-cache's included. Under --icache-compression
# off the I-cache has no compressor at all, so Kagura is left with only the
# D-cache to act on -- that run is what isolates its mechanism from ACC's
# blind spot.
_COMPRESSORS = []
if _COMPRESS_I:
    _COMPRESSORS.append(system.cpu.icache.compressor)
if _COMPRESS_D:
    _COMPRESSORS.append(system.cpu.dcache.compressor)


# Memory bus and connections
system.membus = SystemXBar()

# CPU request ports -> cache cpu_side (response) ports
system.cpu.icache.cpu_side = system.cpu.icache_port
system.cpu.dcache.cpu_side = system.cpu.dcache_port

# Cache mem_side (request) ports -> bus cpu_side_ports (response)
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


# NvmMemCtrl (src/ehs/) just exposes the byte counters MemCtrl already keeps,
# so the controller can price the traffic. Behaviourally a stock MemCtrl.
system.mem_ctrl = NvmMemCtrl()
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
CKPT_E_PER_BYTE = args.e_nvm_write
CKPT_T_PER_BYTE = 150e-9 / 32     # s/byte, from Table I tWR

# Architectural state a checkpoint has to save.
CKPT_REG_BYTES = (
    68
    + (2 * len(_COMPRESSORS) if args.compression in ("acc", "kagura") else 0)
    + (17 if args.compression == "kagura" else 0)
    # The persistent perceptron's learned state is architectural too: H+1
    # weight bytes plus the packed H-bit history. The volatile variant loses
    # it at every failure and so adds nothing -- that asymmetry IS the
    # experiment.
    + ((args.perc_history + 1 + (args.perc_history + 7) // 8)
       if args.compression == "kagura" and args.rm_perceptron
       and not args.perc_volatile else 0)
)

# Kagura's controller is a subclass: same capacitor, voltage monitor, and
# checkpoint pricing, plus the five registers, the 2-bit counter, the
# commit-probe listeners, and the mode broadcast.
_CONTROLLER = (
    KaguraController if args.compression == "kagura" else IntermittentController
)

_KAGURA_PARAMS = (
    dict(
        n_thres_init=args.n_thres,
        closeness_frac=args.closeness_frac,
        aimd=not args.no_aimd,
        thres_cap_frac=args.thres_cap,
        rm_confidence_min=args.rm_confidence,
        sat_counter_init=args.sat_init,
        rm_perceptron=args.rm_perceptron,
        perceptron_history=args.perc_history,
        perceptron_theta=args.perc_theta,
        perceptron_volatile=args.perc_volatile,
    )
    if args.compression == "kagura"
    else {}
)

# The gate's probe: a bare BDI on no cache and in no EnergyCompressor, so it
# cannot reach the capacitor or the energy ledger by construction.
#
# It is parented under the system and passed by reference, not built inline in
# the controller's params. BDI's sub-compressors resolve block_size through a
# Parent.cache_line_size proxy, and the proxy walks ancestors for an attribute
# literally called cache_line_size, which only a System has. Built inline the
# walk hits Root and dies with "Can't resolve proxy 'cache_line_size' ... from
# 'intermittent.ckpt_probe_compressor.compressors0'". Setting block_size on
# the top-level BDI does not help -- it renames nothing on the children.
_PROBE_PARAMS = {}
if args.ckpt_gate:
    system.ckpt_probe = BDI(block_size=args.cacheline)
    _PROBE_PARAMS = dict(ckpt_probe_compressor=system.ckpt_probe,
                         vector_dump_path=args.vector_dump)

root.intermittent = _CONTROLLER(cpu=system.cpu, capacitance=args.capacitance,
                                v_max=args.v_max,
                                v_on=args.v_on, v_off=args.v_off,
                                p_harvest=5e-3, harvest_period=10e-3,
                                duty_cycle=0.5,
                                trace_file=args.trace_file,
                                trace_scale=args.trace_scale,
                                p_static=args.p_static, e_per_inst=args.e_per_inst,
                                ckpt_tags=[system.cpu.dcache.tags],
                                e_per_byte_nvm=CKPT_E_PER_BYTE,
                                t_per_byte_nvm=CKPT_T_PER_BYTE,
                                ckpt_time=args.ckpt_time,
                                ckpt_reg_bytes=CKPT_REG_BYTES,
                                compressors=_COMPRESSORS,
                                nvm=system.mem_ctrl,
                                e_per_byte_nvm_read=args.e_nvm_read,
                                e_per_byte_nvm_write=args.e_nvm_write,
                                nvm_energy_to_capacitor=args.nvm_to_capacitor,
                                **_PROBE_PARAMS,
                                **_KAGURA_PARAMS)
root.intermittent.clk_domain = SrcClockDomain(clock="1GHz", voltage_domain=VoltageDomain())

# Kagura tunes R_thres by AIMD on eviction pressure, which only a cache can
# see: the D-cache reports every eviction and the controller counts the ones
# after the decision point. Under --no-aimd the count is still gathered, it
# just no longer moves the threshold -- which is how a pinned run can still
# say what the rule would have done at that point on the curve.
if args.compression == "kagura":
    system.cpu.dcache.kagura = root.intermittent

m5.instantiate()

# Power-cycle loop. At v_off the controller exits the simulation loop with
# cause "power failure". The system is then drained (in-flight accesses
# complete, which is the clean boundary a JIT checkpoint needs), dirty SRAM
# lines are written back to NVM, the volatile caches are invalidated, and the
# CPU is suspended. The next m5.simulate() runs the event loop with the core
# dark so the capacitor recharges; the controller wakes the CPU at v_on.
#
# powerOff() runs before the writeback: it prices the checkpoint by counting
# dirty blocks, and memWriteback() clears each dirty bit as it saves the
# block, so counting afterwards would always find nothing.
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
