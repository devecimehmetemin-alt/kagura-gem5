"""Fill a McPAT XML template with activity counts from a gem5 stats.txt.

McPAT needs about twenty <stat> values to compute dynamic power. Editing
them by hand is how you end up with a run that silently used somebody
else's numbers, so this does it in one pass and reports every field it
could not find rather than leaving a stale value in place.

Only <stat> entries are touched. Every <param> -- geometry, technology,
device type -- stays exactly as the template has it, because those are
design decisions rather than measurements.

Usage:
    python configs/kagura/mcpat_fill.py m5out/stats.txt kagura.xml kagura_filled.xml
"""
import re
import sys


def read_stats(path):
    out = {}
    with open(path) as f:
        for line in f:
            if line.startswith("-") or not line.strip():
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            try:
                out[parts[0]] = float(parts[1])
            except ValueError:
                pass
    return out


def get(s, *names):
    """First of these stats that exists, else None."""
    for n in names:
        if n in s:
            return s[n]
    return None


def build(s):
    """gem5 stats -> {component id: {stat name: value}}."""
    insts = get(s, "simInsts")
    cycles = get(s, "system.cpu.numCycles")
    branches = get(s, "system.cpu.executeStats0.numBranches")
    loads = get(s, "system.cpu.executeStats0.numLoadInsts")
    stores = get(s, "system.cpu.executeStats0.numStoreInsts")
    int_reads = get(s, "system.cpu.executeStats0.numIntRegReads")
    int_writes = get(s, "system.cpu.executeStats0.numIntRegWrites")
    ialu = get(s, "system.cpu.executeStats0.numIntAluAccesses")
    fp_writes = get(s, "system.cpu.executeStats0.numFpRegWrites") or 0

    # The workload is soft-float, so FP is zero and every FP instruction
    # is really an integer one. Integer count is what is left after the
    # branches.
    fp = 0.0
    ints = None
    if insts is not None and branches is not None:
        ints = insts - branches

    mispred = get(s,
                  "system.cpu.branchPred.condIncorrect",
                  "system.cpu.branchPred.mispredicted",
                  "system.cpu.branchPred.condIncorrect::total")

    icache_acc = get(s, "system.cpu.icache.overallAccesses::total")
    icache_miss = get(s, "system.cpu.icache.overallMisses::total")
    dc_rd_acc = get(s, "system.cpu.dcache.ReadReq.accesses::total")
    dc_rd_miss = get(s, "system.cpu.dcache.ReadReq.misses::total")
    dc_wr_acc = get(s, "system.cpu.dcache.WriteReq.accesses::total")
    dc_wr_miss = get(s, "system.cpu.dcache.WriteReq.misses::total")

    duty = None
    if insts is not None and cycles:
        duty = insts / cycles

    core = {
        "total_instructions": insts,
        "int_instructions": ints,
        "fp_instructions": fp,
        "branch_instructions": branches,
        "branch_mispredictions": mispred,
        "load_instructions": loads,
        "store_instructions": stores,
        "committed_instructions": insts,
        "committed_int_instructions": ints,
        "committed_fp_instructions": fp,
        "pipeline_duty_cycle": duty,
        "total_cycles": cycles,
        "idle_cycles": 0.0,
        "busy_cycles": cycles,
        "int_regfile_reads": int_reads,
        "int_regfile_writes": int_writes,
        "float_regfile_reads": 0.0,
        "float_regfile_writes": fp_writes,
        "ialu_accesses": ialu,
        "fpu_accesses": 0.0,
        "cdb_alu_accesses": ialu,
        "cdb_fpu_accesses": 0.0,
        "function_calls": 0.0,
        "context_switches": 0.0,
    }

    # The top-level system block carries its own cycle counts, and McPAT
    # divides total energy by those to get average power. Leave them at the
    # template's value and every dynamic number is wrong by the ratio of
    # the two -- which is how a 13 W figure appeared for a 200 MHz core.
    system = {
        "total_cycles": cycles,
        "idle_cycles": 0.0,
        "busy_cycles": cycles,
    }

    return {
        "system": system,
        "system.core0": core,
        "system.core0.icache": {
            "read_accesses": icache_acc,
            "read_misses": icache_miss,
            "conflicts": 0.0,
        },
        "system.core0.dcache": {
            "read_accesses": dc_rd_acc,
            "read_misses": dc_rd_miss,
            "write_accesses": dc_wr_acc,
            "write_misses": dc_wr_miss,
            "conflicts": 0.0,
        },
        "system.core0.itlb": {
            "total_accesses": icache_acc,
            "total_misses": 0.0,
            "conflicts": 0.0,
        },
        "system.core0.dtlb": {
            "total_accesses": (dc_rd_acc + dc_wr_acc)
            if (dc_rd_acc is not None and dc_wr_acc is not None) else None,
            "total_misses": 0.0,
            "conflicts": 0.0,
        },
        "system.core0.predictor": {
            "predictor_accesses": branches,
        },
        "system.core0.BTB": {
            "read_accesses": branches,
            "write_accesses": mispred,
        },
    }


COMPONENT = re.compile(r'<component\s+id="([^"]+)"')
STAT = re.compile(r'(<stat\s+name=")([^"]+)("\s+value=")([^"]*)(")')


def fill(xml_path, out_path, wanted):
    """Rewrite <stat> values in place, tracking which component we are in."""
    written = []
    missing = []
    unmatched = {c: set(v) for c, v in wanted.items()}
    current = None

    with open(xml_path) as f:
        lines = f.readlines()

    for i, line in enumerate(lines):
        m = COMPONENT.search(line)
        if m:
            current = m.group(1)

        sm = STAT.search(line)
        if not sm or current not in wanted:
            continue

        name = sm.group(2)
        if name not in wanted[current]:
            continue

        value = wanted[current][name]
        unmatched[current].discard(name)
        if value is None:
            missing.append("%s.%s" % (current, name))
            continue

        # McPAT wants integers everywhere except the duty cycle.
        text = ("%.6f" % value) if name == "pipeline_duty_cycle" \
            else "%d" % round(value)
        lines[i] = STAT.sub(
            lambda mm: mm.group(1) + mm.group(2) + mm.group(3) + text
            + mm.group(5), line)
        written.append("%s.%s = %s" % (current, name, text))

    with open(out_path, "w") as f:
        f.writelines(lines)

    return written, missing, unmatched


def main():
    if len(sys.argv) != 4:
        print(__doc__)
        return 1

    stats_path, xml_path, out_path = sys.argv[1:4]
    stats = read_stats(stats_path)
    if not stats:
        print("no stats parsed from %s" % stats_path)
        return 1

    wanted = build(stats)
    written, missing, unmatched = fill(xml_path, out_path, wanted)

    for w in written:
        print("  set  %s" % w)
    print("\n%d stats written to %s" % (len(written), out_path))

    if missing:
        print("\nNOT FOUND in gem5 stats, left at template value:")
        for m in missing:
            print("  %s" % m)

    leftover = [(c, n) for c, ns in unmatched.items() for n in ns]
    if leftover:
        print("\nNo such <stat> in the template (component may be absent):")
        for c, n in leftover:
            print("  %s.%s" % (c, n))

    return 0


if __name__ == "__main__":
    sys.exit(main())
