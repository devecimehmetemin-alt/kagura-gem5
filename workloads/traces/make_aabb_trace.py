#!/usr/bin/env python3
"""Generate the AABB structured-harvest power trace.

Counterpart to make_rf_trace.py's bursty trace: where that one models a
memoryless harvest (the estimator's worst case), this one is built to have
exactly the temporal structure a one-cycle-history estimator can be scored
on. On-phases repeat in matched pairs of two lengths -- 10, 10, 30, 30 ms --
so power-cycle lengths run A,A,B,B and the estimate outcome alternates
close (second of a pair) / off (the jump between pairs) every cycle.

That alternation is the 2-bit confidence counter's worst case and a
one-weight pattern for the perceptron gate (see rm_perceptron in
KaguraController.py), which is what sweep.sh percgate measures on it.

Two sizing constraints, both learned the hard way:

  - Troughs are 10 ms because the 4.7 uF capacitor the percgate rows use
    rides out anything shorter (a 5 ms trough costs ~11 uJ against ~13.5 uJ
    of usable charge; a first attempt produced a zero-failure run).
  - Run it with --sat-init 3 (SAT_INIT=3 through sweep.sh). At the default
    init of 1 the R_adjust correction switches on at the first pair jump
    and its extrapolation error keeps the counter low and the correction
    firing -- a self-perpetuating spiral that destroys the alternation
    (measured: 3 rewards vs 16 punishments despite 9 of 10 pairs matching
    within 10%).

Same format as rf_bursty.txt: time_s power_W, one sample per 0.2 ms, 20 s
total, looped by the reader. Deterministic by construction.
"""

DT = 0.2e-3        # logging interval, s
DURATION = 20.0    # trace length, s
P_ON = 5.0e-3      # burst power, W (the square wave's peak)

# (duration_s, power_W) per super-period: A, A, B, B on-phases, each
# followed by a trough long enough to force a power failure.
BLOCKS = [(10e-3, P_ON), (10e-3, 0.0),
          (10e-3, P_ON), (10e-3, 0.0),
          (30e-3, P_ON), (10e-3, 0.0),
          (30e-3, P_ON), (10e-3, 0.0)]

with open("rf_aabb.txt", "w", newline="\n") as f:
    f.write("# AABB structured-harvest trace (see make_aabb_trace.py in"
            " this directory).\n")
    f.write("# Format: time_s power_W, one sample per %.1f ms, %g s total.\n"
            % (DT * 1e3, DURATION))
    t = 0.0
    n_on = n = 0
    while t < DURATION:
        for dur, p in BLOCKS:
            for _ in range(int(round(dur / DT))):
                f.write("%.4f %.6e\n" % (t, p))
                t += DT
                n += 1
                n_on += p > 0

print("wrote rf_aabb.txt: %d samples, duty %.1f%%, mean %.4g W"
      % (n, 100.0 * n_on / n, P_ON * n_on / n))
