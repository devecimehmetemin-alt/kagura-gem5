#!/usr/bin/env python3
"""Shape traces at high power-failure rates, capacitor held at 4.7 uF.

The capacitor rides out ~6 ms, so the trough cannot go below ~7 ms and the
period cannot go below ~10 ms. With wall_time = E_total / P_mean fixed at
~0.83 s by the default 3.333 mW harvest, that caps failures at ~83.

The only remaining lever is to STARVE the harvest: lower P_mean stretches
wall time, so more 10 ms periods fit inside the same fixed work. Constraint:
p_on = P_mean / duty must exceed the ~2.96 mW the CPU draws, or the capacitor
never reaches the wake threshold and the run never completes.

  usage: make_dense_traces.py <on_scale> <trough_ms> <mean_uW>
"""
import sys

SCALE   = float(sys.argv[1])
TROUGH  = float(sys.argv[2]) * 1e-3
MEAN    = float(sys.argv[3]) * 1e-6
E_TOTAL = 2280e-6          # measured system energy for compfit_stream
DURATION_MULT = 1.15       # trace must outlast the run before looping

SHAPES = {
    "flat":     [20.0, 20.0, 20.0, 20.0],
    "aabb":     [10.0, 10.0, 30.0, 30.0],
    "aaabbb":   [10.0, 10.0, 10.0, 30.0, 30.0, 30.0],
    "sawtooth": [20.0, 18.5, 17.0, 15.5],
    "burst":    [10.0, 10.0, 10.0, 30.0],
    "burst5":   [10.0, 10.0, 10.0, 50.0],
}

wall = E_TOTAL / MEAN
tag  = "d%d" % round(MEAN * 1e6)

for name, raw in SHAPES.items():
    on_ms = [m / SCALE for m in raw]
    duty  = sum(on_ms) * 1e-3 / (sum(on_ms) * 1e-3 + len(on_ms) * TROUGH)
    p_on  = MEAN / duty
    period = (sum(on_ms) / len(on_ms)) + TROUGH * 1e3
    fails  = wall / (period * 1e-3)
    flag   = "" if p_on > 2.96e-3 else "   *** p_on BELOW 2.96 mW, WILL STALL"

    dt = min(min(on_ms) * 1e-3, TROUGH) / 10.0
    blocks = []
    for ms in on_ms:
        blocks.append((ms * 1e-3, p_on))
        blocks.append((TROUGH, 0.0))

    path = "rf_%s_%s.txt" % (name, tag)
    with open(path, "w", newline="\n") as f:
        f.write("# %s dense trace: on/%g, trough %.2f ms, mean %.1f uW,"
                " C stays 4.7e-6 (make_dense_traces.py)\n"
                % (name, SCALE, TROUGH * 1e3, MEAN * 1e6))
        t = 0.0; n = 0
        while t < wall * DURATION_MULT:
            for dur, p in blocks:
                for _ in range(int(round(dur / dt))):
                    f.write("%.7f %.6e\n" % (t, p)); t += dt; n += 1
    print("%-20s %8d samples  duty %5.2f%%  p_on %6.3f mW  period %5.2f ms"
          "  wall %5.2f s  -> ~%.0f failures%s"
          % (path, n, duty * 100, p_on * 1e3, period, wall, fails, flag))
