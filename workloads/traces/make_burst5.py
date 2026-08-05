#!/usr/bin/env python3
"""burst5: the burst shape at a 5:1 ratio and matched duty.

rf_burst.txt uses on-phases [10,10,10,30] ms, which gives a 60% duty --
the only shape in the family not at 66.7%, so it is not energy-matched to
flat/aabb/aaabbb despite the MEAN_POWER scaling. [10,10,10,50] restores the
match (80 ms on, 4 x 10 ms trough = 120 ms super-period, 66.7% duty, 5.000 mW
peak) while raising the burst ratio from 3:1 to 5:1.

The point is the estimator's overrun. Kagura predicts the long cycle from the
preceding short one, so the long cycle runs past R_prev by its full excess:
20 ms at 3:1, 40 ms at 5:1. Everything after that point is Regular Mode.
Outcome period stays 4, so the pattern remains learnable.
"""

DT = 0.2e-3
DURATION = 20.0
TROUGH = 10e-3
MEAN_POWER = 5.0e-3 * 2.0 / 3.0

ON_MS = [10.0, 10.0, 10.0, 50.0]

duty = sum(ON_MS) * 1e-3 / (sum(ON_MS) * 1e-3 + len(ON_MS) * TROUGH)
p_on = MEAN_POWER / duty

blocks = []
for ms in ON_MS:
    blocks.append((ms * 1e-3, p_on))
    blocks.append((TROUGH, 0.0))

path = "rf_burst5.txt"
with open(path, "w", newline="\n") as f:
    f.write("# burst5 harvest trace (see make_burst5.py in this directory).\n")
    f.write("# On-phases (ms): %s, each followed by a %.0f ms trough;"
            " %.3f mW peak for a %.3f mW mean.\n"
            % (", ".join("%g" % m for m in ON_MS), TROUGH * 1e3,
               p_on * 1e3, MEAN_POWER * 1e3))
    f.write("# Format: time_s power_W, one sample per %.1f ms, %g s total.\n"
            % (DT * 1e3, DURATION))
    t = 0.0
    n = n_on = 0
    while t < DURATION:
        for dur, p in blocks:
            for _ in range(int(round(dur / DT))):
                f.write("%.4f %.6e\n" % (t, p))
                t += DT
                n += 1
                n_on += p > 0

print("wrote %s: %d samples, duty %.1f%%, peak %.3f mW, mean %.4g W,"
      " super-period %.1f ms"
      % (path, n, 100.0 * n_on / n, p_on * 1e3, p_on * n_on / n,
         sum(ON_MS) + len(ON_MS) * TROUGH * 1e3))
