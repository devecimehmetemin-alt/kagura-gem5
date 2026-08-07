#!/usr/bin/env python3
"""Decompose an inference's energy, and price what compressed weight storage
would save.

Reads the stats.txt of each `sweep.sh wstream` run and answers the question
the campaign could not answer from the literature: how much of an inference's
energy is the weight fetch from NVM?

Run after:  bash configs/kagura/sweep.sh wstream
Usage:      python3 configs/kagura/wstream_report.py [--outroot sweep]

Stats are read from each run's stats.txt rather than from the shared
results.csv, because the decomposition needs simInsts and the core-energy
knobs, which the CSV schema does not carry -- and adding columns to it would
misalign every row already appended under the old header.

MEASURED VERSUS PROJECTED
-------------------------
Measured: every energy term below, priced by the same calibrated knobs as the
rest of the campaign.

Projected: the saving from compressed weight storage. No compressed-NVM
mechanism is simulated. A codec of ratio R moves 1/R of the bytes, so it saves
nvmReadEnergy * (1 - 1/R) -- reported at the ratios the offline audit actually
measured on MLPerf-Tiny weights. Every such figure is labelled `proj`.

The projection is deliberately optimistic in one respect and conservative in
another, and they do not cancel:
  optimistic -- it assumes the whole NVM read stream is weights. On this
                microbenchmark that is nearly true; on a real model there is
                also activation and code traffic that a weight codec does not
                touch, so the real saving is lower.
  conservative -- it charges nothing for decompression. A byte-granular
                decoder is ~1 pJ against the ~64 pJ miss it rides on, so this
                is a small error, but it is in compression's favour.
"""

import argparse
import glob
import os
import re
import sys

TICKS_PER_SEC = 1e12

# These MUST match what sweep_wstream passed, because stats.txt does not
# record them: the core term is the denominator of the fetch share, so a
# mismatch silently misprices the headline. sweep_wstream runs the Table I
# regime (as tracedpaper does), NOT stage_six_sweep.py's 80 pJ / 0.5 mW
# stage-2 placeholders -- those would inflate core energy ~3.6x and understate
# the weight-fetch share by the same factor.
E_PER_INST_DEFAULT = 22e-12
P_STATIC_DEFAULT = 50e-6


def read_stats(path):
    """gem5 stats.txt -> {name: float}. First occurrence wins."""
    out = {}
    pat = re.compile(r"^(\S+)\s+([-\d.eE+nan]+)")
    with open(path) as f:
        for line in f:
            if line.startswith("-") or not line.strip():
                continue
            m = pat.match(line)
            if m and m.group(1) not in out:
                try:
                    out[m.group(1)] = float(m.group(2))
                except ValueError:
                    pass
    return out


def decompose(s, e_per_inst, p_static, inferences):
    """Energy terms for one run, in joules per inference."""
    insts = s.get("simInsts", 0.0)
    sim_ticks = s.get("simTicks", 0.0)
    off_ticks = s.get("intermittent.ticksPoweredOff", 0.0)
    on_secs = max(sim_ticks - off_ticks, 0.0) / TICKS_PER_SEC

    terms = {
        "core": insts * e_per_inst,
        "static": on_secs * p_static,
        "nvm_read": s.get("intermittent.nvmReadEnergy", 0.0),
        "nvm_write": s.get("intermittent.nvmWriteEnergy", 0.0),
        "checkpoint": s.get("intermittent.checkpointEnergy", 0.0),
        "compression": s.get("intermittent.compressionEnergy", 0.0),
    }
    total = sum(terms.values())
    per_inf = {k: v / inferences for k, v in terms.items()}
    return per_inf, total / inferences, {
        "insts": insts / inferences,
        "read_bytes": s.get("intermittent.nvmReadBytes", 0.0) / inferences,
        "failures": s.get("intermittent.numPowerFailures", 0.0),
    }


# Ratios measured offline on MLPerf-Tiny weights (traffic-weighted, 32 B
# lines, zero_rle). See tinyml_audit_results.md / tinyml_prune_sweep.md.
CODEC_RATIOS = [
    ("dense (as shipped)", 1.00),
    ("70% pruned", 2.00),
    ("vww, 84% sparse", 4.79),
    ("90% pruned", 5.36),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outroot", default="sweep")
    ap.add_argument("--e-per-inst", type=float, default=E_PER_INST_DEFAULT)
    ap.add_argument("--p-static", type=float, default=P_STATIC_DEFAULT)
    args = ap.parse_args()

    # Directories only: the sweep leaves a wstream-r<N>.log beside each run
    # dir, and those match the glob too.
    dirs = [d for d in glob.glob(os.path.join(args.outroot, "wstream-r*"))
            if os.path.isdir(d) and re.search(r"r(\d+)$", d)]
    dirs.sort(key=lambda d: int(re.search(r"r(\d+)$", d).group(1)))
    if not dirs:
        sys.exit("no wstream runs under %s/ -- run: bash configs/kagura/"
                 "sweep.sh wstream" % args.outroot)

    print("# Where does an inference's energy go?")
    print()
    print("Energy per inference, by term. `reuse` is MACs per weight byte: "
          "1 = fully-connected at batch=1, ~36 = 1x1 conv on a 6x6 map, "
          "144 = a larger conv. Weight traffic is fixed at 64 kB/inference; "
          "compute scales with reuse.")
    print()
    header = ["reuse", "uJ/inf", "core", "static", "nvm_read", "nvm_write",
              "ckpt", "comp", "**fetch share**", "fails"]
    print("| " + " | ".join(header) + " |")
    print("|" + "---|" * len(header))

    rows = []
    for d in dirs:
        stats_path = os.path.join(d, "stats.txt")
        if not os.path.exists(stats_path):
            print("skipping %s: no stats.txt" % d, file=sys.stderr)
            continue
        reuse = int(re.search(r"r(\d+)$", d).group(1))

        # inferences came from BENCH_OPTS; recover it from the run log so the
        # per-inference normalisation cannot silently use the wrong divisor.
        infs = 1
        log = d + ".log"
        if os.path.exists(log):
            with open(log, errors="replace") as f:
                m = re.search(r"wstream reuse=(\d+) inferences=(\d+)", f.read())
                if m:
                    infs = int(m.group(2))
                    if int(m.group(1)) != reuse:
                        print("WARNING: %s label says reuse=%d but the binary "
                              "ran reuse=%s" % (d, reuse, m.group(1)),
                              file=sys.stderr)

        per_inf, total, extra = decompose(read_stats(stats_path),
                                          args.e_per_inst, args.p_static, infs)
        if total <= 0:
            continue
        share = per_inf["nvm_read"] / total
        rows.append((reuse, total, share))

        cells = ["%d" % reuse, "%.2f" % (total * 1e6)]
        for k in ("core", "static", "nvm_read", "nvm_write", "checkpoint",
                  "compression"):
            cells.append("%.0f%%" % (100.0 * per_inf[k] / total))
        cells.append("**%.1f%%**" % (100.0 * share))
        cells.append("%.0f" % extra["failures"])
        print("| " + " | ".join(cells) + " |")

    if not rows:
        sys.exit("no usable runs")

    # ---------------------------------------------------------- projection
    print()
    print("## Projected saving from compressed weight storage")
    print()
    print("`proj` = nvm_read share x (1 - 1/R). Not simulated -- no "
          "compressed-NVM mechanism exists in the model yet. Read these as "
          "an upper bound on what building one could return.")
    print()
    print("| reuse | fetch share | " +
          " | ".join("proj @ %s (%.2fx)" % (n, r) for n, r in CODEC_RATIOS) +
          " |")
    print("|" + "---|" * (2 + len(CODEC_RATIOS)))
    for reuse, _total, share in rows:
        cells = ["%d" % reuse, "%.1f%%" % (100.0 * share)]
        for _name, r in CODEC_RATIOS:
            cells.append("%.1f%%" % (100.0 * share * (1.0 - 1.0 / r)))
        print("| " + " | ".join(cells) + " |")

    # ------------------------------------------------------------- verdict
    print()
    print("## Verdict")
    print()
    best = max(r for _, _, r in rows)
    worst = min(r for _, _, r in rows)
    conv = [s for u, _, s in rows if u >= 16]
    conv_share = sum(conv) / len(conv) if conv else 0.0
    proj_conv = conv_share * (1.0 - 1.0 / 4.79)

    print("Weight-fetch share ranges from **%.1f%%** (compute-bound, high "
          "reuse) to **%.1f%%** (traffic-bound, fully-connected)."
          % (100.0 * worst, 100.0 * best))
    print()
    print("At convolutional reuse (>=16), fetch averages **%.1f%%** of "
          "inference energy, so a 4.79x codec projects to **%.1f%%** of total "
          "energy saved." % (100.0 * conv_share, 100.0 * proj_conv))
    print()
    if proj_conv >= 0.05:
        print("That clears the ~5% the TinyML direction was pitched at. The "
              "leg is worth building: next step is a real compressed-NVM "
              "weight path, not another projection.")
    elif proj_conv >= 0.02:
        print("Below the ~5% pitch but not negligible. The mechanism is real "
              "and small -- it would be a section, not a headline, and the "
              "case for it has to rest on the theory rather than the number.")
    else:
        print("Under 2%: the weight-fetch term is too small a share for a "
              "codec on it to matter, whatever the ratio. That falsifies the "
              "TinyML leg on THIS machine's price sheet -- report it, and "
              "check the NVM read energy bracket before concluding, since "
              "every figure here is linear in --e-nvm-read.")
    print()
    print("Reminder: every energy number is linear in --e-nvm-read "
          "(default 2e-12 J/B, published bracket 1.3e-12 to 8e-12). Rerun at "
          "the bracket ends before quoting a single figure.")


if __name__ == "__main__":
    main()
