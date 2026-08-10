"""Where the energy in a power cycle actually goes.

Answers one question: what share of the budget does each term take, and
therefore what is the ceiling on a policy that only touches one of them.

Cache compression can act on two rows. It spends compressionEnergy, and it
can reduce nvmEnergy by keeping more blocks resident. So compressionFrac is
the most Kagura could ever save by scheduling compression better, and
nvmEnergyFrac is the most compression could save if it were free.

Usage:
    python configs/kagura/energy_report.py m5out/stats.txt
    python configs/kagura/energy_report.py sweep/*/stats.txt
"""
import sys
import os


# Every term the capacitor pays for. Order is print order.
TERMS = [
    ("dynamicEnergy", "Switching (core + regfile + SRAM, lumped)"),
    ("nvmEnergy", "NVM traffic (misses and writebacks)"),
    ("staticEnergy", "Leakage"),
    ("checkpointEnergy", "Checkpoints"),
    ("compressionEnergy", "Compression"),
]

# Controller stats. The prefix depends on where the controller was parented,
# so match on the trailing name instead of hardcoding "root.intermittent.".
CTRL = [
    "numPowerFailures",
    "ticksPoweredOff",
    "budgetEnergy",
    "checkpointBytes",
    "checkpointDirtyBytes",
    "nvmReadBytes",
    "nvmWriteBytes",
    "nvmEnergyOffPeriod",
] + [k for k, _ in TERMS]

# Global stats, which carry no prefix.
GLOBAL = ["simInsts", "simTicks"]


def read_stats(path):
    """Pull the stats we need out of a gem5 stats.txt."""
    out = {}
    with open(path) as f:
        for line in f:
            if line.startswith("-") or not line.strip():
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            name, value = parts[0], parts[1]

            key = None
            if name in GLOBAL:
                key = name
            elif "intermittent" in name:
                tail = name.rsplit(".", 1)[-1]
                if tail in CTRL:
                    key = tail

            if key is not None:
                try:
                    out[key] = float(value)
                except ValueError:
                    pass
    return out


def si(joules):
    """Joules with a sensible prefix. These span nJ to mJ."""
    for scale, unit in ((1e-3, "mJ"), (1e-6, "uJ"), (1e-9, "nJ"),
                        (1e-12, "pJ")):
        if abs(joules) >= scale:
            return "%8.3f %s" % (joules / scale, unit)
    return "%8.3f fJ" % (joules / 1e-15)


def report(path):
    s = read_stats(path)
    if not s:
        print("%s: no stats found" % path)
        return

    total = s.get("budgetEnergy", 0.0)
    if total <= 0:
        print("%s: budgetEnergy is zero -- was this run built after the "
              "energy-breakdown stats were added?" % path)
        return

    print("=" * 68)
    print(path)
    print("=" * 68)

    failures = s.get("numPowerFailures", 0.0)
    insts = s.get("simInsts", 0.0)
    if failures > 0:
        print("Power failures      %d" % failures)
        print("Instructions        %d total, %.0f per power cycle"
              % (insts, insts / failures))
        print("Energy per cycle    %s" % si(total / failures))
    else:
        print("Instructions        %d (no power failures in this run)" % insts)
    print("Total energy        %s" % si(total))
    print()

    print("%-42s %12s %8s" % ("Term", "Energy", "Share"))
    print("-" * 68)
    rows = [(s.get(k, 0.0), label) for k, label in TERMS]
    for value, label in sorted(rows, reverse=True):
        print("%-42s %12s %7.3f%%"
              % (label, si(value), 100.0 * value / total))
    print("-" * 68)
    print("%-42s %12s %7.3f%%" % ("Total", si(total), 100.0))
    print()

    # The numbers the ceiling argument rests on.
    comp = s.get("compressionEnergy", 0.0) / total
    nvm = s.get("nvmEnergy", 0.0) / total
    ckpt = s.get("checkpointEnergy", 0.0) / total
    print("Ceilings")
    print("  Scheduling compression better (Kagura)  <= %.3f%%"
          % (100.0 * comp))
    print("  Compression at zero cost, all misses    <= %.3f%%"
          % (100.0 * nvm))
    print("  Anything acting on the checkpoint       <= %.3f%%"
          % (100.0 * ckpt))
    print()

    # Stages 2-5 build a plain MemCtrl and never pass nvm= to the controller,
    # so the traffic term is structurally absent rather than measured as zero.
    # Without it the denominator is too small and every share above is an
    # overestimate. Only stage_six_sweep.py wires NvmMemCtrl.
    if s.get("nvmReadBytes", 0.0) == 0 and s.get("nvmWriteBytes", 0.0) == 0:
        print("WARNING: no NVM traffic recorded. This config did not connect")
        print("         a NvmMemCtrl to the controller, so main-memory energy")
        print("         is missing from the budget entirely. Re-run with")
        print("         configs/kagura/stage_six_sweep.py for a complete one.")
        print()

    off = s.get("nvmEnergyOffPeriod", 0.0)
    if off > 0:
        print("Note: %s of NVM energy was counted while the core was dark and"
              % si(off))
        print("      never actually drained the capacitor (%.2f%% of total)."
              % (100.0 * off / total))
        print()


def main():
    paths = sys.argv[1:]
    if not paths:
        paths = ["m5out/stats.txt"]
    missing = [p for p in paths if not os.path.isfile(p)]
    for p in missing:
        print("not found: %s" % p)
    for p in paths:
        if os.path.isfile(p):
            report(p)


if __name__ == "__main__":
    main()
