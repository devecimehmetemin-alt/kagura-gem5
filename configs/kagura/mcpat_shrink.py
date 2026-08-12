"""Cut a McPAT core description down to a five-stage in-order MCU.

McPAT's area comes entirely from the <param> entries -- the structure sizes.
Activity stats do not enter it, so changing the gem5 CPU model does nothing
to the reported area. The template here started as ARM_A9_2GHz.xml, and an
A9 carries a branch predictor, a BTB, 32-entry TLBs, an instruction window
and load/store queues. The paper's core is "single-core in-order five-stage"
at 0.538 mm2 including caches, and an ARMv7-M part has no MMU and no branch
predictor at all.

This sets every structure that a machine like that would not have, or would
have far smaller. Only <param> entries are touched, and only ones already
present -- anything missing is reported rather than invented.

Usage:
    python configs/kagura/mcpat_shrink.py kagura_filled.xml kagura_mcu.xml
    python configs/kagura/mcpat_shrink.py --keep-predictor in.xml out.xml
"""
import argparse
import re


# Structures an in-order MCU either lacks or holds a few entries of. Values
# are the smallest McPAT still models: it divides by several of these, so
# zero is only safe where the parameter is a switch rather than a size.
CORE = {
    # Single issue, every stage one instruction wide.
    "fetch_width": "1",
    "decode_width": "1",
    "issue_width": "1",
    "peak_issue_width": "1",
    "commit_width": "1",
    "pipeline_depth": "5,5",

    # No out-of-order machinery. machine_type=1 already says in-order, but
    # the sizes are still read for area unless they are zeroed.
    "instruction_window_size": "2",
    "fp_instruction_window_size": "1",
    "ROB_size": "0",
    "rename_scheme": "0",

    # A small fetch queue instead of an A9's.
    "instruction_buffer_size": "4",
    "decoded_stream_buffer_size": "2",

    # 16 architectural registers, no renaming, so the physical file is the
    # architectural one. No FP register file worth the name.
    "archi_Regs_IRF_size": "16",
    "phy_Regs_IRF_size": "16",
    "archi_Regs_FRF_size": "1",
    "phy_Regs_FRF_size": "1",

    # One load and one store outstanding: this core blocks on a miss.
    "load_buffer_size": "2",
    "store_buffer_size": "2",
    "memory_ports": "1",
    "RAS_size": "0",

    "number_hardware_threads": "1",

    # Function units. The A9 template gives three integer ALUs, which on a
    # single-issue in-order core is two more than can ever be busy and cost
    # 0.078 mm2 of the 0.81 mm2 total. One integer ALU, one multiplier (a
    # Cortex-M3 has a single-cycle multiply), no FPU.
    "ALU_per_core": "1",
    "MUL_per_core": "1",
    "FPU_per_core": "0",
    "fp_issue_width": "0",
}

# prediction_width 0 tells McPAT the core has no branch predictor, so the
# predictor and BTB components are skipped entirely. A Cortex-M3 has none.
NO_PREDICTOR = {"prediction_width": "0"}

# ARMv7-M has an MPU, not an MMU, so there is no TLB to model. McPAT will
# not accept zero entries, so this is the floor rather than the truth.
TLB = {"number_entries": "4"}

WANTED = {
    "system.core0": dict(CORE),
    "system.core0.itlb": dict(TLB),
    "system.core0.dtlb": dict(TLB),
}

COMPONENT = re.compile(r'<component\s+id="([^"]+)"')
PARAM = re.compile(r'(<param\s+name=")([^"]+)("\s+value=")([^"]*)(")')


def shrink(xml_path, out_path, wanted):
    changed = []
    same = []
    missing = {c: set(v) for c, v in wanted.items()}
    current = None

    with open(xml_path) as f:
        lines = f.readlines()

    for i, line in enumerate(lines):
        m = COMPONENT.search(line)
        if m:
            current = m.group(1)

        pm = PARAM.search(line)
        if not pm or current not in wanted:
            continue

        name = pm.group(2)
        if name not in wanted[current]:
            continue

        old = pm.group(4)
        new = wanted[current][name]
        missing[current].discard(name)

        if old == new:
            same.append("%s.%s = %s" % (current, name, old))
            continue

        lines[i] = PARAM.sub(
            lambda mm: mm.group(1) + mm.group(2) + mm.group(3) + new
            + mm.group(5), line)
        changed.append("%s.%s  %s -> %s" % (current, name, old, new))

    with open(out_path, "w") as f:
        f.writelines(lines)

    return changed, same, missing


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("infile")
    ap.add_argument("outfile")
    ap.add_argument("--keep-predictor", action="store_true",
                    help="Leave the branch predictor and BTB in place. Use "
                    "this if McPAT refuses prediction_width=0.")
    args = ap.parse_args()

    wanted = {c: dict(v) for c, v in WANTED.items()}
    if not args.keep_predictor:
        wanted["system.core0"].update(NO_PREDICTOR)

    changed, same, missing = shrink(args.infile, args.outfile, wanted)

    for c in changed:
        print("  %s" % c)
    print("\n%d params changed, %d already correct -> %s"
          % (len(changed), len(same), args.outfile))

    leftover = [(c, n) for c, ns in missing.items() for n in ns]
    if leftover:
        print("\nNot present in the template (left alone):")
        for c, n in leftover:
            print("  %s.%s" % (c, n))


if __name__ == "__main__":
    main()
