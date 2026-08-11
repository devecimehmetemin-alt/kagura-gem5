"""Where the energy in a power cycle actually goes.

Answers one question: what share of the budget does each term take, and
therefore what is the ceiling on a policy that only touches one of them.

Two of the five terms are split further than the controller stats go.

NVM traffic splits into reads and writes, which the controller already
tracks separately, and the reads are attributed to the two caches by their
miss counts (an I-cache miss and a D-cache miss both pull one line).

Core dynamic energy splits into L1 SRAM access and everything else. The
model charges a single lumped e_per_inst per committed instruction, which
folds the SRAM access in, so this is a decomposition of that term and not
an addition to it: Table I gives 9 pJ per SRAM access, gem5 counts the
accesses, and the remainder is core logic.

What cannot be split without a per-structure power model: leakage (one
lumped p_static for the whole system, so the cache's share of it is not
recoverable) and core logic by pipeline stage.

Usage:
    python configs/kagura/energy_report.py m5out/stats.txt
    python configs/kagura/energy_report.py sweep/*/stats.txt
"""
import sys
import os


# Table I: SRAM access energy. The only core-side energy the paper gives.
E_SRAM_ACCESS = 9e-12

CACHE_LINE = 32

# Controller stats. The prefix depends on where the controller was parented,
# so match on the trailing name instead of hardcoding "root.intermittent.".
CTRL = [
    "numPowerFailures",
    "ticksPoweredOff",
    "budgetEnergy",
    "checkpointBytes",
    "checkpointDirtyBytes",
    "checkpointEnergy",
    "compressionEnergy",
    "staticEnergy",
    "dynamicEnergy",
    "nvmReadBytes",
    "nvmWriteBytes",
    "nvmReadEnergy",
    "nvmWriteEnergy",
    "nvmEnergy",
    "nvmEnergyOffPeriod",
]

# Global stats, which carry no prefix.
GLOBAL = ["simInsts", "simTicks", "simSeconds"]

# Cache stats, matched on the full name.
CACHES = ["icache", "dcache"]
CACHE_FIELDS = ["overallAccesses", "overallMisses", "overallHits"]


def read_stats(path):
    """Pull the stats we need out of a gem5 stats.txt."""
    out = {}
    cache_names = {}
    for c in CACHES:
        for f in CACHE_FIELDS:
            cache_names["system.cpu.%s.%s::total" % (c, f)] = "%s.%s" % (c, f)

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
            elif name in cache_names:
                key = cache_names[name]
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


def row(label, value, total, indent=0):
    print("%-44s %12s %8.3f%%"
          % (" " * indent + label, si(value), 100.0 * value / total))


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
    if s.get("simSeconds"):
        print("Simulated time      %.6f s" % s["simSeconds"])
    print("Total energy        %s" % si(total))
    print()

    # Carve the L1 access energy out of the lumped dynamic term.
    accesses = sum(s.get("%s.overallAccesses" % c, 0.0) for c in CACHES)
    cache_dyn = accesses * E_SRAM_ACCESS
    dynamic = s.get("dynamicEnergy", 0.0)
    core_logic = dynamic - cache_dyn
    split_ok = accesses > 0 and core_logic > 0

    nvm_read = s.get("nvmReadEnergy", 0.0)
    nvm_write = s.get("nvmWriteEnergy", 0.0)
    static = s.get("staticEnergy", 0.0)
    comp = s.get("compressionEnergy", 0.0)
    ckpt = s.get("checkpointEnergy", 0.0)

    print("%-44s %12s %8s" % ("Term", "Energy", "Share"))
    print("-" * 68)
    if split_ok:
        row("Core: logic (dynamic minus SRAM access)", core_logic, total)
        row("Core: L1 SRAM accesses", cache_dyn, total, indent=2)
    else:
        row("Core: dynamic (lumped, not split)", dynamic, total)
    row("NVM read (cache fills)", nvm_read, total)
    row("NVM write (dirty writebacks)", nvm_write, total)
    row("Leakage (whole system, lumped)", static, total)
    row("Compression", comp, total)
    row("Checkpoints", ckpt, total)
    print("-" * 68)
    print("%-44s %12s %8.3f%%" % ("Total", si(total), 100.0))
    print()

    # Which cache pulled the read traffic. Both caches fill a whole line on
    # a miss, so misses divide the read energy between them.
    misses = {c: s.get("%s.overallMisses" % c, 0.0) for c in CACHES}
    total_misses = sum(misses.values())
    if total_misses > 0 and nvm_read > 0:
        print("NVM read traffic by source")
        for c in CACHES:
            m = misses[c]
            acc = s.get("%s.overallAccesses" % c, 0.0)
            rate = (100.0 * m / acc) if acc else 0.0
            print("  %-20s %10d misses (%5.1f%% miss rate)  %s  %5.1f%%"
                  % (c, m, rate, si(nvm_read * m / total_misses),
                     100.0 * m / total_misses))
        print("  %-20s %10d lines x %d B = %.1f MB"
              % ("total", total_misses, CACHE_LINE,
                 total_misses * CACHE_LINE / 1e6))
        print()

    print("Ceilings")
    print("  Scheduling compression better (Kagura)  <= %.3f%%"
          % (100.0 * comp / total))
    print("  Removing all miss traffic               <= %.3f%%"
          % (100.0 * (nvm_read + nvm_write) / total))
    print("  Removing all L1 access energy           <= %.3f%%"
          % (100.0 * cache_dyn / total))
    print("  Anything acting on the checkpoint       <= %.3f%%"
          % (100.0 * ckpt / total))
    print()

    # Leakage is the one input with nothing published to check it against,
    # so quote the bound that survives setting it to zero. Any positive
    # leakage only makes these shares smaller, which makes this the ceiling
    # no assumption about p_static can raise.
    work = total - static
    if work > 0:
        print("Leakage-independent bounds (p_static set to zero)")
        print("  Work energy, excluding leakage           %s" % si(work))
        print("  Compression                             <= %.3f%%"
              % (100.0 * comp / work))
        print("  Miss traffic                            <= %.3f%%"
              % (100.0 * (nvm_read + nvm_write) / work))
        print("  Checkpoints                             <= %.3f%%"
              % (100.0 * ckpt / work))
        print("  Every input above is Table I or measured; no placeholder")
        print("  for leakage enters these numbers.")
        print()

    # Stages 2-5 build a plain MemCtrl and never pass nvm= to the controller,
    # so the traffic term is structurally absent rather than measured as zero.
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
        print("      never actually drained the capacitor (%.3f%% of total)."
              % (100.0 * off / total))
        print()


def main():
    paths = sys.argv[1:]
    if not paths:
        paths = ["m5out/stats.txt"]
    for p in paths:
        if not os.path.isfile(p):
            print("not found: %s" % p)
    for p in paths:
        if os.path.isfile(p):
            report(p)


if __name__ == "__main__":
    main()
