#!/usr/bin/env python3
"""Generate an RFHome-scale ambient RF harvest trace.

Replaces make_rf_trace.py for paper-comparison runs. That script calibrates
to 2.5 mW -- the square wave's average, chosen so its runs stay comparable to
the square-wave sweeps -- which is roughly 500x the ambient power the paper
actually harvests. At 2.5 mW the capacitor refills in ~4.8 ms and the machine
runs at ~12% duty; at the paper's scale it refills in ~2.4 s and runs at
~0.03% duty. Those are different regimes, and the second one is where
compressed blocks reliably die before they are read again -- the condition
the whole design is premised on.

WHAT WAS READ OFF FIG. 11 (left panel, "RFHome")

  - axis 0..24 uW; dense ink filling from zero up to a wandering ceiling
  - typical ceiling ~8-10 uW, busier stretches ~18 uW, rare spikes ~24 uW
  - the ceiling moves on a multi-second scale: a quiet stretch around
    7-13 s, a busier one around 14-20 s
  - long-run mean therefore ~5 uW. This is read off a figure, not a table,
    so treat it as 5 +/- 2 and re-run at 3 and 8 uW if a result turns on it.
  - sampled every 10 us (the paper's P_avg = E_10us / 10us), ~30 s long

The recording itself ships with NVPsim and is not publicly distributed. If it
is obtained later it drops into --trace-file unchanged and this file retires.

MODEL

    p(t) = A(t) * U,   U ~ Uniform(0, 2)

  - A(t) is a slow lognormal envelope, cosine-interpolated between knots
    drawn every ENV_KNOT seconds. This is the part that matters. At 5 uW the
    capacitor integrates ~240,000 samples per power cycle, so every structure
    faster than about a second averages out completely and contributes
    nothing but its mean. Cycle-to-cycle variation -- precisely what Kagura's
    estimator bets on being small -- comes entirely from the envelope. A
    generator without one produces a constant-power source in disguise, which
    would make the estimator look good for the wrong reason.
  - U is uniform with mean 1 and a hard ceiling at twice the local envelope,
    so the drawn band top sits at 2*A(t). With A spanning ~2-12 uW that
    reproduces Fig. 11's 4-24 uW band directly.

The trace is rescaled at the end so its long-run mean is exactly MEAN_POWER;
the shape parameters therefore only set the shape, never the energy budget.

Deterministic: fixed seed, so the trace is reproducible from this script
alone. Output is ~66 MB -- generate it on the machine that runs it, do not
commit it.
"""

import math
import random

DT = 10e-6          # logging interval, s (the paper's 10 us)
DURATION = 30.0     # trace length, s (Fig. 11's x-axis runs to 3e6 x 10 us)
MEAN_POWER = 5.0e-6  # calibration target, W

ENV_KNOT = 1.5      # envelope correlation time, s
ENV_SIGMA = 0.35    # envelope lognormal sigma; sets the quiet/busy contrast.
                    # Tuned against Fig. 11: 0.35 puts the per-second mean
                    # inside a ~3x quiet-to-busy range and the peak near
                    # 24 uW. 0.5 overshot both (7.8x and 30 uW).
FAST_MAX = 2.0      # U ~ Uniform(0, FAST_MAX), so mean 1, ceiling 2*A

# Capacitor band, only used for the sanity line printed at the end. Matches
# the config's defaults (stage_six_sweep.py: v_on=2.4, v_off=1.8) and Table I's
# 4.7 uF, so the printed charge time is the one the runs will actually see.
CAP_F = 4.7e-6
V_ON, V_OFF = 2.4, 1.8

SEED = 63
OUT = "rfhome.txt"

rng = random.Random(SEED)

n_samples = int(round(DURATION / DT))
n_knots = int(math.ceil(DURATION / ENV_KNOT)) + 2
knots = [rng.lognormvariate(0.0, ENV_SIGMA) for _ in range(n_knots)]


def envelope(t):
    """Cosine-interpolated knots: smooth, so the envelope has no corners the
    capacitor would see as steps."""
    x = t / ENV_KNOT
    i = int(x)
    f = x - i
    w = 0.5 - 0.5 * math.cos(math.pi * f)
    return knots[i] * (1.0 - w) + knots[i + 1] * w


samples = [envelope(k * DT) * rng.uniform(0.0, FAST_MAX)
           for k in range(n_samples)]

scale = MEAN_POWER / (sum(samples) / len(samples))
samples = [p * scale for p in samples]

with open(OUT, "w", newline="\n") as f:
    f.write("# RFHome-scale ambient RF harvest trace (see"
            " make_rfhome_trace.py in this directory).\n")
    f.write("# Format: time_s power_W, one sample per %g us, %g s total,"
            " mean %.4g W.\n" % (DT * 1e6, DURATION, MEAN_POWER))
    for i, p in enumerate(samples):
        # 6 decimals is the minimum that keeps 10 us steps strictly
        # increasing, which loadTrace() checks for.
        f.write("%.6f %.6e\n" % (i * DT, p))

peak = max(samples)
band_j = 0.5 * CAP_F * (V_ON * V_ON - V_OFF * V_OFF)
print("wrote %s: %d samples, mean %.4g W, peak %.4g W"
      % (OUT, len(samples), MEAN_POWER, peak))
print("capacitor band %.3g uJ -> %.3g s to charge at the mean"
      % (band_j * 1e6, band_j / MEAN_POWER))
