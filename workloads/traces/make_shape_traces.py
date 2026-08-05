#!/usr/bin/env python3
"""Generate the structured-harvest trace family for the gate comparison.

Companion to make_rf_trace.py (memoryless bursty) and make_aabb_trace.py (the
period-2 alternating case). These add the longer-lag and control shapes, so
the gate comparison spans a range of autocorrelation structures rather than
one constructed point.

The hypothesis under test: Kagura's estimator assumes the next power cycle
resembles the last one (high lag-1 autocorrelation). A perceptron over
outcome history should help exactly where lag-1 similarity is weak but a
longer lag is strong.

WHY BINARY OUTCOME HISTORY IS ENOUGH. The gate's perceptron does not see
cycle lengths -- it sees whether each cycle's estimate landed "close"
(within closeness_frac of the estimate) or not. A periodic length sequence
still yields a *periodic close/off sequence*, so a longer-period trace is
learnable without giving the predictor numeric inputs. Each shape below is
designed for the outcome period it induces, not just the length period:

  flat      20,20,20,20      outcomes: all close        -- Kagura's best case
  aabb      10,10,30,30      outcomes: period 2         -- (make_aabb_trace.py)
  burst     10,10,10,30      outcomes: period 4         -- 3 quiet, 1 burst
  sawtooth  20,18.5,17,15.5  outcomes: period 4         -- gentle ramp + reset

`flat` is the control that matters most: lag-1 similarity is high, so Kagura
should be at its best and the perceptron should merely do no harm. A gate
that only ever wins is not being tested.

`sawtooth` is the closest of these to a physically plausible harvest (the
paper's Fig. 11 thermal trace is a repeated decline with an abrupt reset).
Its ramp steps are deliberately gentle: a step must stay inside the 10%
closeness window so the ramp cycles read "close" and only the reset reads
"off". Steeper ramps make every cycle read "off", which degenerates into
"veto always" and tests nothing.

SIZING CONSTRAINTS, both learned the hard way:

  - Troughs are 10 ms. The 4.7 uF capacitor rides out anything shorter (a
    5 ms trough costs ~11 uJ against ~13.5 uJ of usable charge), and a trace
    that never causes a power failure produces a zero-cycle run.
  - Run with --sat-init 3 (SAT_INIT=3 via sweep.sh). At the default init of
    1, Kagura's R_adjust correction arms on the first jump and its
    extrapolation error keeps it armed, destroying the outcome pattern.
  - Super-periods are kept near ~100 ms so that the ~0.8 s of simulated time
    a benchmark takes contains enough power cycles (~20-40) for a predictor
    to warm up and be scored.

VERIFY BEFORE TRUSTING. Periodic power does not guarantee periodic committed
memory operations -- the pattern has to survive the capacitor, the voltage
thresholds and the workload. Dump the per-cycle counts first:

  gem5 ... --debug-flags=Kagura --debug-file=kag.log ... --trace-file <t>
  grep CHECKPOINT kag.log | grep -o "R_mem=[0-9]*"

and check the sequence repeats with the intended period before spending a
sweep on it.

Same format as the other traces: time_s power_W, one sample per 0.2 ms,
20 s total, looped by the reader. Deterministic by construction.
"""

DT = 0.2e-3        # logging interval, s
DURATION = 20.0    # trace length, s
TROUGH = 10e-3     # off-phase, s -- must exceed the capacitor ride-through

# Every shape is scaled to this long-run mean, which is rf_aabb.txt's own
# (5 mW at 2/3 duty). Without it the shapes deliver different total energy --
# their duty cycles differ by design -- and a cross-trace comparison would be
# measuring the energy budget rather than the policy. Matching aabb's figure
# rather than make_rf_trace.py's 2.5 mW keeps these directly comparable to the
# aabb results already collected.
MEAN_POWER = 5.0e-3 * 2.0 / 3.0

# shape name -> on-phase durations (ms) making up one super-period
SHAPES = {
    "flat":     [20.0, 20.0, 20.0, 20.0],
    "burst":    [10.0, 10.0, 10.0, 30.0],
    "sawtooth": [20.0, 18.5, 17.0, 15.5],
}


def write_shape(name, on_ms):
    # Scale the on-phase power so the long-run mean matches every other shape
    # (the duty cycle differs per shape, so a fixed peak would not).
    duty = sum(on_ms) * 1e-3 / (sum(on_ms) * 1e-3 + len(on_ms) * TROUGH)
    p_on = MEAN_POWER / duty

    blocks = []
    for ms in on_ms:
        blocks.append((ms * 1e-3, p_on))
        blocks.append((TROUGH, 0.0))

    path = "rf_%s.txt" % name
    with open(path, "w", newline="\n") as f:
        f.write("# %s harvest trace (see make_shape_traces.py in this"
                " directory).\n" % name)
        f.write("# On-phases (ms): %s, each followed by a %.0f ms trough;"
                " %.3f mW peak for a %.3f mW mean.\n"
                % (", ".join("%g" % m for m in on_ms), TROUGH * 1e3,
                   p_on * 1e3, MEAN_POWER * 1e3))
        f.write("# Format: time_s power_W, one sample per %.1f ms, %g s"
                " total.\n" % (DT * 1e3, DURATION))
        t = 0.0
        n = n_on = 0
        while t < DURATION:
            for dur, p in blocks:
                for _ in range(int(round(dur / DT))):
                    f.write("%.4f %.6e\n" % (t, p))
                    t += DT
                    n += 1
                    n_on += p > 0

    period_ms = sum(on_ms) + len(on_ms) * TROUGH * 1e3
    print("wrote %s: %d samples, duty %.1f%%, peak %.3f mW, mean %.4g W,"
          " super-period %.1f ms"
          % (path, n, 100.0 * n_on / n, p_on * 1e3, p_on * n_on / n,
             period_ms))


for name, on_ms in SHAPES.items():
    write_shape(name, on_ms)
