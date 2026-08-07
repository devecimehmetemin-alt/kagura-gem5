#!/usr/bin/env python3
"""Verdict for the checkpoint-compression gate.

Reads the stats.txt of each `sweep.sh ckptgate` run and answers: is the
dirty set a checkpoint drains compressible, and would compressing it save
enough to matter? Two numbers per row, because either alone misleads:

  ratio       what BDI achieved on the dirty bytes (traffic-weighted).
              A data property; independent of every energy knob.
  materiality the saved NVM write energy as a share of the run's total
              capacitor drain. A 3x ratio on a 200 B checkpoint is a
              trivial win; this is the number that says so.

Run after:  bash configs/kagura/sweep.sh ckptgate
Usage:      python3 configs/kagura/ckptgate_report.py [--outroot sweep]

The controls are the instrument check, not data: dirtyzero must read near
BDI's zero-line ceiling and dirtyrand ~1.0x, or every benchmark row is
untrustworthy. The report enforces both, plus the internal consistency
check that the probe walked exactly the population the checkpoint priced
(ckptProbeUncompBytes == checkpointDirtyBytes under an uncompressed
cache).

Energy defaults MUST match what sweep_ckptgate passed (Table I core:
22 pJ/inst, 50 uW; NVM write 10 pJ/B) -- stats.txt does not record them,
and the materiality denominator is priced from them.

Pre-registered thresholds, set before any run (benchmark rows, >=2 kB):
  PROCEED    ratio >= 1.5x AND materiality >= 1% at some size
  FALSIFIED  ratio <= 1.1x everywhere
  GRAY       anything else -- decide on the >=2x block distribution
"""

import argparse
import glob
import os
import re
import sys

TICKS_PER_SEC = 1e12

E_PER_INST_DEFAULT = 22e-12
P_STATIC_DEFAULT = 50e-6
E_NVM_WRITE_DEFAULT = 10e-12

CONTROLS = ("dirtyzero", "dirtyrand")

GATE_RATIO_PROCEED = 1.5
GATE_RATIO_FALSIFY = 1.1
GATE_MATERIALITY = 0.01          # 1% of total drain
MATERIAL_SIZE_MIN = 2048         # thresholds apply at >= 2 kB


def read_stats(path):
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


def size_bytes(s):
    m = re.fullmatch(r"(\d+)(k?)B", s)
    return int(m.group(1)) * (1024 if m.group(2) else 1)


def total_drain(s, e_per_inst, p_static):
    on_secs = max(s.get("simTicks", 0.0)
                  - s.get("intermittent.ticksPoweredOff", 0.0), 0.0) \
        / TICKS_PER_SEC
    return (s.get("simInsts", 0.0) * e_per_inst
            + on_secs * p_static
            + s.get("intermittent.nvmReadEnergy", 0.0)
            + s.get("intermittent.nvmWriteEnergy", 0.0)
            + s.get("intermittent.checkpointEnergy", 0.0)
            + s.get("intermittent.compressionEnergy", 0.0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outroot", default="sweep")
    ap.add_argument("--e-per-inst", type=float, default=E_PER_INST_DEFAULT)
    ap.add_argument("--p-static", type=float, default=P_STATIC_DEFAULT)
    ap.add_argument("--e-nvm-write", type=float, default=E_NVM_WRITE_DEFAULT)
    args = ap.parse_args()

    dirs = [d for d in glob.glob(os.path.join(args.outroot, "ckptgate-*"))
            if os.path.isdir(d)]
    if not dirs:
        sys.exit("no ckptgate runs under %s/ -- run: bash configs/kagura/"
                 "sweep.sh ckptgate" % args.outroot)

    rows = []
    problems = []
    for d in dirs:
        m = re.search(r"ckptgate-(\d+k?B)-(.+)$", os.path.basename(d))
        stats_path = os.path.join(d, "stats.txt")
        if not m or not os.path.exists(stats_path):
            problems.append("skipped %s (no stats.txt or bad label)" % d)
            continue
        size, bench = m.group(1), m.group(2)
        s = read_stats(stats_path)

        # An empty stats.txt means the run died before simulating (missing
        # binary, config fatal, crash) -- it must not read as a measurement.
        # "Probe saw no dirty blocks" and "gem5 never ran" are different
        # diagnoses with different fixes; conflating them cost a debug cycle.
        if "simTicks" not in s:
            problems.append("%s/%s: no stats were dumped -- the run died "
                            "before simulating; read %s.log"
                            % (size, bench, os.path.basename(d)))
            continue

        blocks = s.get("intermittent.ckptProbeBlocks", 0.0)
        unc = s.get("intermittent.ckptProbeUncompBytes", 0.0)
        comp = s.get("intermittent.ckptProbeCompBytes", 0.0)
        dirty = s.get("intermittent.checkpointDirtyBytes", 0.0)
        fails = s.get("intermittent.numPowerFailures", 0.0)
        if blocks == 0:
            problems.append("%s/%s: probe saw no dirty blocks" % (size, bench))
            continue

        # Self-check: the probe must have walked exactly the population the
        # checkpoint priced. Under --compression none both count full lines.
        if abs(unc - dirty) > 0.5:
            problems.append(
                "%s/%s: ckptProbeUncompBytes (%d) != checkpointDirtyBytes "
                "(%d) -- probe and checkpoint disagree about the dirty set"
                % (size, bench, unc, dirty))

        saved_j = (unc - comp) * args.e_nvm_write
        drain = total_drain(s, args.e_per_inst, args.p_static)
        rows.append({
            "bench": bench, "size": size, "sbytes": size_bytes(size),
            "ratio": unc / comp if comp else 0.0,
            "zero": s.get("intermittent.ckptProbeZeroBlocks", 0.0) / blocks,
            "ge2x": s.get("intermittent.ckptProbeGe2x", 0.0) / blocks,
            "ge4x": s.get("intermittent.ckptProbeGe4x", 0.0) / blocks,
            "dirty_per_ckpt": unc / fails if fails else 0.0,
            "materiality": saved_j / drain if drain > 0 else 0.0,
            "fails": fails,
        })

    rows.sort(key=lambda r: (r["bench"] not in CONTROLS, r["bench"],
                             r["sbytes"]))

    print("# Checkpoint-compression gate: is the dirty set compressible?")
    print()
    print("Ratio is traffic-weighted over probed dirty bytes. Materiality = "
          "saved NVM write energy / total capacitor drain (Table I core "
          "pricing). `dirty/ckpt` is the average dirty bytes one checkpoint "
          "drains -- the absolute stakes.")
    print()
    hdr = ["bench", "size", "ratio", "zero blks", ">=2x", ">=4x",
           "dirty/ckpt", "fails", "**materiality**"]
    print("| " + " | ".join(hdr) + " |")
    print("|" + "---|" * len(hdr))
    for r in rows:
        print("| %s | %s | %.2fx | %.0f%% | %.0f%% | %.0f%% | %.0f B | %.0f "
              "| **%.2f%%** |"
              % (r["bench"], r["size"], r["ratio"], 100 * r["zero"],
                 100 * r["ge2x"], 100 * r["ge4x"], r["dirty_per_ckpt"],
                 r["fails"], 100 * r["materiality"]))
    print()

    # ------------------------------------------------------ instrument check
    print("## Instrument check")
    print()
    ok = True
    zero_rows = [r for r in rows if r["bench"] == "dirtyzero"]
    rand_rows = [r for r in rows if r["bench"] == "dirtyrand"]
    if not zero_rows or not rand_rows:
        print("**Controls missing** -- benchmark rows are unvalidated.")
        ok = False
    else:
        zmin = min(r["ratio"] for r in zero_rows)
        rmax = max(r["ratio"] for r in rand_rows)
        if zmin < 4.0:
            print("**dirtyzero read only %.2fx** (expect near BDI's zero-line "
                  "ceiling): the probe is not seeing the win it was handed."
                  % zmin)
            ok = False
        if rmax > 1.05:
            print("**dirtyrand read %.2fx** (expect ~1.0x): the probe is "
                  "manufacturing compressibility -- likely reading stale or "
                  "zeroed data." % rmax)
            ok = False
        if ok:
            print("Controls bracket correctly (dirtyzero >= %.2fx, dirtyrand "
                  "<= %.2fx)." % (zmin, rmax))
    for p in problems:
        print("- WARNING: %s" % p)
        ok = False
    print()

    # --------------------------------------------------------------- verdict
    print("## Verdict")
    print()
    bench_rows = [r for r in rows if r["bench"] not in CONTROLS
                  and r["sbytes"] >= MATERIAL_SIZE_MIN]
    if not bench_rows:
        print("No benchmark rows at >= 2 kB -- nothing to gate on.")
        return
    if not ok:
        print("**Instrument check failed: no verdict.** Fix the probe first; "
              "any number below would be a property of the bug.")
        return

    best = max(bench_rows, key=lambda r: r["ratio"])
    passing = [r for r in bench_rows
               if r["ratio"] >= GATE_RATIO_PROCEED
               and r["materiality"] >= GATE_MATERIALITY]
    all_flat = all(r["ratio"] <= GATE_RATIO_FALSIFY for r in bench_rows)

    print("Best benchmark row: %s @ %s -- %.2fx, materiality %.2f%%."
          % (best["bench"], best["size"], best["ratio"],
             100 * best["materiality"]))
    print()
    if passing:
        print("**PROCEED.** %d row(s) clear both pre-registered bars "
              "(ratio >= %.1fx, materiality >= %.0f%%). The dirty set is NOT "
              "the fill stream's incompressible population -- build the "
              "mechanism. Check `zero blks` first: if most of the win is "
              "zero lines, the RTL shrinks from full BDI to a zero-detector, "
              "which re-scopes the hardware weeks."
              % (len(passing), GATE_RATIO_PROCEED, 100 * GATE_MATERIALITY))
    elif all_flat:
        print("**FALSIFIED.** Ratio <= %.1fx on every benchmark row at "
              ">= 2 kB: written data is as incompressible as read data on "
              "this suite, and checkpoint-time compression has nothing to "
              "work with. Record it and pivot to model-grounding -- one day "
              "spent, five weeks saved." % GATE_RATIO_FALSIFY)
    else:
        print("**GRAY.** Neither bar cleared outright. Decide on the "
              "distribution: if the >=2x blocks carry a meaningful share of "
              "bytes, a selective scheme (compress only what pays) may still "
              "clear materiality -- otherwise treat as falsified.")
    print()
    print("Reminder: materiality is linear in --e-nvm-write (%.0e J/B here; "
          "published bracket ~5e-12 to 8e-11). The ratio is not -- it is a "
          "property of the data." % args.e_nvm_write)


if __name__ == "__main__":
    main()
