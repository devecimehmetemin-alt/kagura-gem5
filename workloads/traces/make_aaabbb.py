#!/usr/bin/env python3
"""Period-3 outcome trace: runs of three at two levels.

Lengths 10,10,10,30,30,30 give lag-1 outcomes O,C,C,O,C,C -- period 3,
33% off. Duty 66.7% matches rf_aabb.txt, so peak power is identical and
the two are directly comparable. Constants mirror make_shape_traces.py.
"""
DT, DURATION, TROUGH = 0.2e-3, 20.0, 10e-3
MEAN_POWER = 5.0e-3 * 2.0 / 3.0
ON_MS = [10.0, 10.0, 10.0, 30.0, 30.0, 30.0]

duty = sum(ON_MS) * 1e-3 / (sum(ON_MS) * 1e-3 + len(ON_MS) * TROUGH)
p_on = MEAN_POWER / duty
blocks = [(m * 1e-3, p_on) for m in ON_MS]
blocks = [b for m in ON_MS for b in ((m * 1e-3, p_on), (TROUGH, 0.0))]

with open("rf_aaabbb.txt", "w", newline="\n") as f:
    f.write("# aaabbb harvest trace (see make_aaabbb.py in this directory).\n")
    f.write("# On-phases (ms): %s, each followed by a %.0f ms trough;"
            " %.3f mW peak for a %.3f mW mean.\n"
            % (", ".join("%g" % m for m in ON_MS), TROUGH * 1e3,
               p_on * 1e3, MEAN_POWER * 1e3))
    f.write("# Format: time_s power_W, one sample per %.1f ms, %g s total.\n"
            % (DT * 1e3, DURATION))
    t = n = n_on = 0.0, 0, 0
    t, n, n_on = 0.0, 0, 0
    while t < DURATION:
        for dur, p in blocks:
            for _ in range(int(round(dur / DT))):
                f.write("%.4f %.6e\n" % (t, p))
                t += DT; n += 1; n_on += p > 0
print("wrote rf_aaabbb.txt: %d samples, duty %.1f%%, peak %.3f mW,"
      " super-period %.0f ms"
      % (n, 100.0 * n_on / n, p_on * 1e3,
         sum(ON_MS) + len(ON_MS) * TROUGH * 1e3))
