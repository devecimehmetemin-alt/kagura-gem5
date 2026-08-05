#!/usr/bin/env python3
"""Generate a synthetic bursty RF-harvest power trace.

Stand-in for the RFHome recording used by the paper (Sec. VIII, Fig. 11):
that trace ships with NVPsim [63] and is not publicly distributed, so this
produces a trace with the same *shape* -- ambient indoor RF arrives in short
bursts (a nearby transmitter keying up) separated by idle troughs, some of
them long -- in the same format the paper describes ("average power values
in a text file", one entry per fixed logging interval). If the real RFHome
is obtained later it drops into --trace-file unchanged and this file is
retired.

Model: a two-state renewal process.
  - burst durations ~ Exp(mean 4 ms), floored at 0.4 ms
  - idle durations  ~ mixture: 75% Exp(mean 3 ms), 25% Exp(mean 20 ms)
    (the long-trough tail is the point: whether the capacitor can ride a
    trough out is what makes cycle length energy-limited)
  - burst power     ~ lognormal (sigma 0.6), constant within a burst
  - idle power      = 0

The whole trace is then scaled so its long-run mean equals 2.5 mW -- the
square wave's average (5 mW x 50% duty) -- so results are comparable to the
square-wave runs at equal energy budget, and runtimes stay in the same
range. Sampled every 0.2 ms for 20 s and looped by the reader; the paper
uses the same loop-a-recording approach to keep energy input consistent
across configurations.

Deterministic: fixed seed, so the committed trace is reproducible from this
script alone.
"""

import random

DT = 0.2e-3        # logging interval, s
DURATION = 20.0    # trace length, s
MEAN_POWER = 2.5e-3  # calibration target, W (the square wave's average)

BURST_MEAN = 4e-3
BURST_MIN = 0.4e-3
IDLE_SHORT_MEAN = 3e-3
IDLE_LONG_MEAN = 20e-3
IDLE_LONG_FRAC = 0.25
SIGMA = 0.6

rng = random.Random(63)

# Build the on/off timeline first, as (end_time, power) segments.
segments = []
t = 0.0
# Start mid-idle so the trace does not begin with a synchronized burst.
state_burst = False
while t < DURATION:
    if state_burst:
        dur = max(rng.expovariate(1.0 / BURST_MEAN), BURST_MIN)
        power = rng.lognormvariate(0.0, SIGMA)
    else:
        mean = (IDLE_LONG_MEAN
                if rng.random() < IDLE_LONG_FRAC else IDLE_SHORT_MEAN)
        dur = rng.expovariate(1.0 / mean)
        power = 0.0
    t += dur
    segments.append((t, power))
    state_burst = not state_burst

# Sample it at the logging interval.
samples = []
seg = 0
t = 0.0
while t < DURATION:
    while segments[seg][0] <= t:
        seg += 1
    samples.append(segments[seg][1])
    t += DT

# Calibrate the mean.
mean = sum(samples) / len(samples)
scale = MEAN_POWER / mean
samples = [p * scale for p in samples]

with open("rf_bursty.txt", "w", newline="\n") as f:
    f.write("# Synthetic bursty RF-harvest power trace (see make_rf_trace.py"
            " in this directory).\n")
    f.write("# Format: time_s power_W, one sample per %.1f ms, %g s total,"
            " mean %.4g W.\n" % (DT * 1e3, DURATION, MEAN_POWER))
    for i, p in enumerate(samples):
        f.write("%.4f %.6e\n" % (i * DT, p))

on = sum(1 for p in samples if p > 0)
print("wrote rf_bursty.txt: %d samples, duty %.1f%%, mean %.4g W, "
      "peak %.4g W" % (len(samples), 100.0 * on / len(samples),
                       sum(samples) / len(samples), max(samples)))
