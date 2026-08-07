# Kagura

A gem5 reproduction of *Intermittence-Aware Cache Compression* (HPCA 2026),
built on gem5 v25.1.0.1.

The target is an energy-harvesting microcontroller. It runs off a capacitor,
loses power when the capacitor drains, and takes a just-in-time checkpoint on
the way down. Cache compression looks attractive there because it shrinks two
things at once: the working set, and the dirty set the checkpoint has to write
to non-volatile memory. But compression itself costs energy the capacitor has
to supply. Kagura's claim is that compression should be switched off near the
predicted end of a power cycle, because a block compressed just before a
failure dies with the SRAM and never earns anything back.

Testing that needs three things gem5 does not have: a capacitor and a
power-failure model, a compressor that costs joules and not only cycles, and
the two compression policies. All of it lives in `src/ehs/`.


---

## The object graph

Seven SimObjects, wired up by the config scripts in `configs/kagura/`. The
shape is a straight line from the CPU down to memory, with the controller
sitting off to the side and reading from everything:

```
    IntermittentMinorCPU          (MinorCPU subclass)
             |
             v
    IntermittentController        (the capacitor)
      KaguraController            (subclass: adds the registers)
             |
             v
    ACC                           (one per cache, holds the GCP)
      EnergyCompressor            (its base: prices compression)
             |
             v
    ACCCache                      (icache, dcache)
             |
             v
    NvmMemCtrl                    (main memory)
```

The interesting part is not the boxes, it is which way information crosses
between them:

| From | To | What crosses |
|---|---|---|
| `IntermittentMinorCPU` | `IntermittentController` | `totalInsts()`, for dynamic energy |
| `IntermittentMinorCPU` | `KaguraController` | `RetiredLoads` / `RetiredStores` probes, one per committed memory op |
| `KaguraController` | `ACC` (both caches) | `broadcastMode()` -> `setRegularMode()` |
| `ACC` | `IntermittentController` | `getEnergy()`, a running total the controller differentiates |
| `ACCCache` | `ACC` | `reward()` / `penalize()` on every hit to a compressed block |
| `ACCCache` | `ACC` | per-fill flags: did this miss serve a store, and at what vaddr |
| `ACCCache` (dcache) | `KaguraController` | `blockEvicted()`, which is what AIMD tunes on |
| `ACCCache` tags | `IntermittentController` | the dirty-block walk at checkpoint (`ckpt_tags`) |
| `NvmMemCtrl` | `IntermittentController` | `bytesRead()` / `bytesWritten()` |
| `IntermittentController` | `IntermittentMinorCPU` | `suspendContext()` / `activateContext()` |

Two loops run over this graph.

**The energy loop, every 1 us.** `updateCapacitor()`
([`intermittent_controller.cc:87`](src/ehs/intermittent_controller.cc:87))
reschedules itself every 1000 cycles of a 1 GHz clock
([`:83`](src/ehs/intermittent_controller.cc:83),
[`:126`](src/ehs/intermittent_controller.cc:126)). Each tick it integrates
harvest power in and consumed power out, turns stored energy into a voltage,
and compares that against `v_off` and `v_on`.

**The power cycle, driven from Python.** The controller cannot suspend a
pipelined CPU asynchronously, since in-flight instructions panic at commit. So
a power failure is a handshake with the config script:

```
updateCapacitor() sees V <= v_off
        |
        +-> freezeCpu() -> exitSimLoop("power failure")    [controller]
                |
                +-> m5.drain()                             [config]
                    root.intermittent.powerOff()           [controller]
                    m5.memWriteback(root)                  [config]
                    m5.memInvalidate(root)                 [config]
                        |
                        +-> m5.simulate() resumes, core dark
                            updateCapacitor() sees V >= v_on
                                |
                                +-> thawCpu()              [controller]
```

`powerOff()` has to run before `memWriteback()`. It prices the checkpoint by
counting dirty blocks, and `memWriteback()` clears each dirty bit as it saves
the block, so counting afterwards would always find nothing.

### One D-cache store, end to end

Both policies only ever act at a fill, so a single store that misses is enough
to touch the whole graph:

1. The store misses in `ACCCache::access()`. An MSHR records it, and the
   request goes over the membus to `NvmMemCtrl`, whose byte counters the
   capacitor reads at the next 1 us tick.
2. The response arrives at `ACCCache::recvTimingResp()`. That reads the MSHR
   first (was this a store, what virtual address) and sets sticky flags on the
   compressor, then lets `Cache::recvTimingResp()` do the fill.
3. Inside the fill the cache calls `ACC::compress()`, which walks its priority
   ladder (hint, Kagura override, GCP, sampler) and either runs BDI, which is
   counted so the capacitor pays for it at the next `getEnergy()` poll, or
   stores the block full size.
4. If BDI halved the block, the tags can co-allocate it with a neighbour.
   Every later hit to that block in `access()` rewards the GCP with the miss
   penalty, or debits decompression latency if the block ended up sitting
   alone.
5. When the block is finally evicted, `evictBlock()` reports to
   `KaguraController`, and AIMD uses that count to retune `R_thres` at the next
   reboot.

The same committed store also fired the `RetiredStores` probe into
`memOpCommitted()`, moving `R_mem` one step closer to the decision point. So
one instruction is both a fill request and one count toward switching
compression off.

---

## `IntermittentController`: the capacitor

[`intermittent_controller.hh`](src/ehs/intermittent_controller.hh),
[`.cc`](src/ehs/intermittent_controller.cc), 490 lines

A `ClockedObject` holding one number that matters, `storedEnergy`. The rest is
bookkeeping around it.

### Energy in

`getHarvestPower()` ([`:183`](src/ehs/intermittent_controller.cc:183)) returns
either a synthetic square wave (`p_harvest`, `harvest_period`, `duty_cycle`) or
a sample from a loaded trace. `loadTrace()`
([`:131`](src/ehs/intermittent_controller.cc:131)) parses `time_s power_W`
pairs, rejects non-monotonic times and negative powers, and scales them by
`trace_scale`. The scale factor is how one measured harvest profile can serve
several power regimes.

### Energy out

`getConsumePower()` ([`:215`](src/ehs/intermittent_controller.cc:215)) adds up
four terms:

| Term | Source |
|---|---|
| static | `p_static`, constant while powered |
| dynamic | `cpu->totalInsts()` delta x `e_per_inst` |
| compression | sum of `compressor->getEnergy()` deltas over the `compressors` vector |
| NVM traffic | `nvm->bytesRead()` / `bytesWritten()` deltas x per-byte energies |

Each one is polled as a delta since the last 1 us tick, then divided by `dt` to
become an average power over the interval
([`:259-261`](src/ehs/intermittent_controller.cc:259)). That is why the
compressors and the memory controller only have to expose running totals. The
controller does the differentiating.

Two details worth knowing. The compression and NVM accounting happens before
the `if (!powered) return 0.0` guard
([`:252`](src/ehs/intermittent_controller.cc:252)), so traffic that happens
while the core is dark is still counted, and it lands in the
`nvmBytesOffPeriod` / `nvmEnergyOffPeriod` stats. And NVM energy is only
actually drained from the capacitor when `nvm_energy_to_capacitor` is set
([`:248`](src/ehs/intermittent_controller.cc:248)). It is always measured;
whether it competes with the program for charge is a sweep axis.

### The checkpoint

`countDirtyBytes()` ([`:282`](src/ehs/intermittent_controller.cc:282)) walks
every tag store in `ckpt_tags` with `forEachBlk`, skips blocks without
`CacheBlk::DirtyBit`, and totals the rest. A compressed block is charged its
`getSizeBits()`, a plain one a full line. Only the D-cache is listed in the
configs, because instruction fills are never written and no I-cache block is
ever dirty.

`powerOff()` ([`:362`](src/ehs/intermittent_controller.cc:362)) is the whole
checkpoint in about thirty lines: optionally run the probe compressor, count
the dirty bytes, add `ckpt_reg_bytes` for architectural state, price it at
`e_per_byte_nvm`, subtract that from `storedEnergy`, recompute the voltage, and
suspend the CPU. If the checkpoint costs more than what is left it warns rather
than quietly losing state ([`:375`](src/ehs/intermittent_controller.cc:375)).
The entire point of `v_off` is to leave enough charge to finish, so that
warning means `v_off` is set too low for this checkpoint size.

`probeDirtyBlocks()` ([`:308`](src/ehs/intermittent_controller.cc:308)) is kept
separate on purpose. It runs a compressor over the dirty set to ask what the
checkpoint would compress to, and that compressor is not attached to any cache
and not wrapped in an `EnergyCompressor`. By construction it cannot reach the
capacitor or the energy ledger.

---

## `IntermittentMinorCPU`

[`intermittent_minor_cpu.cc`](src/ehs/intermittent_minor_cpu.cc), 36 lines

The smallest object here, and it exists for one line that is *not* in it. Stock
`MinorCPU::drainResume()` wakes every suspended thread. Every power cycle
resumes from a drain, so that would quietly power the core back up in the
middle of an outage. This override copies the body and drops the wakeup loop
([`:18-34`](src/ehs/intermittent_minor_cpu.cc:18)), which leaves the decision
where it belongs, in `IntermittentController::thawCpu()` at `v_on`.

It also supplies the two things the controller and Kagura read from the CPU:
`totalInsts()` for dynamic energy, and the `RetiredLoads` / `RetiredStores`
probe points that `MinorCPU` fires once per committed memory operation.

---

## `NvmMemCtrl`

[`nvm_mem_ctrl.cc`](src/ehs/nvm_mem_ctrl.cc), 26 lines

A `MemCtrl` with two accessors that expose counters `MemCtrl` already keeps:

```cpp
uint64_t NvmMemCtrl::bytesRead()    const { return stats.bytesReadSys.value(); }
uint64_t NvmMemCtrl::bytesWritten() const { return stats.bytesWrittenSys.value(); }
```

Behaviourally it is a stock `MemCtrl`. It exists because main-memory traffic is
the largest energy term in this system, and it is the one compression exists to
reduce. A model that charges for compression but not for the traffic
compression avoids would be biased against compression by construction.

---

## `EnergyCompressor`: pricing compression

[`energy_compressor.hh`](src/ehs/energy_compressor.hh),
[`.cc`](src/ehs/energy_compressor.cc), 150 lines

A `compression::Base` that wraps another compressor (BDI in every configuration
here) and adds two things gem5's framework has no concept of: a cost in joules,
and a decision about whether to compress at all.

`setCache()` ([`:29`](src/ehs/energy_compressor.cc:29)) forwards to the wrapped
compressor as well as to the base, so both end up bound to the same cache.

### The two outcomes

Every fill ends in one of two calls.

`runCompressor()` ([`:38`](src/ehs/energy_compressor.cc:38)) rebuilds the cache
line from chunks, hands it to the wrapped compressor's public `compress()`,
records the size it achieved, and increments `compressionsRun`.

`passThrough()` ([`:77`](src/ehs/energy_compressor.cc:77)) sets the size to
`blkSize * 8` bits and both latencies to zero. That is deliberately identical
to what a failed compression produces: the block is stored full size, pays no
decompression latency, and can never co-allocate. Nothing downstream has to
tell "we declined to compress" apart from "compression did not help".

### Energy

`getEnergy()` ([`:123`](src/ehs/energy_compressor.cc:123)) is the whole cost
model:

```cpp
return energyStats.compressionsRun.value() * energyPerCompression +
       stats.decompressions.value() * energyPerDecompression;
```

`compressionsRun` counts compressions that actually ran, which is not the same
as gem5's inherited `compressions` stat, which counts attempts. Decompressions
come from the base class, charged by the cache on every hit to a compressed
block.

### Two policy hooks

`dirtyOverride()` ([`energy_compressor.hh:77`](src/ehs/energy_compressor.hh:77))
is `dirtyAware && fillServesWrite`. It is an exemption that pulls a fill past a
Regular Mode gate when the fill serves a store, on the reasoning that a block
about to become dirty is a block the checkpoint will have to write.

`hintSaysSkip()` ([`energy_compressor.hh:95`](src/ehs/energy_compressor.hh:95))
tests the fill's virtual address against `[sw_hint_lo, sw_hint_hi)`. It is a
software compressibility hint with no lookup cost and no mispredicts, so a run
with it set bounds what any real hint could achieve.

Both need per-fill information the compressor interface cannot carry. `ACCCache`
supplies it with sticky flags set around the base call, described below.

---

## `ACC`: the published baseline

[`acc.hh`](src/ehs/acc.hh), [`.cc`](src/ehs/acc.cc), 139 lines

Adaptive Cache Compression (Alameldeen & Wood, ISCA 2004), written as an
`EnergyCompressor` subclass. One saturating counter, the Global Compression
Predictor, accumulates net cycles saved by compression and gates the wrapped
compressor:

```cpp
bool compressionEnabled() const { return gcp > 0; }   // acc.hh:91
```

`gcp > 0` is Compression Mode, anything else is Regular Mode.

### The decision, in priority order

`ACC::compress()` ([`acc.cc:54`](src/ehs/acc.cc:54)) is the control path that
matters most in this codebase. Five outcomes, checked in this order:

| # | Test | Result |
|---|---|---|
| 1 | `hintSaysSkip()` | `passThrough`. The software hint outranks everything |
| 2 | `kaguraRegular && !dirtyOverride()` | `passThrough`, counted as `kaguraGatedCompressions` |
| 3 | `compressionEnabled()` | `runAndScore`, normal Compression Mode |
| 4 | sampler: `++gatedSinceSample >= sampleInterval` | `runAndScore`, counted as `sampledCompressions` |
| 5 | otherwise | `passThrough`, counted as `gatedCompressions` |

Kagura's override is checked before the sampler (line 65 against line 80), so a
Kagura run suppresses the forced sampling compressions along with the ordinary
ones. `mSAMP.sh` exists to bound what that is worth.

Also worth flagging: `ACC::compress()` overrides
`EnergyCompressor::compress()` without chaining to it. Steps 1 and 2 are
re-implemented rather than inherited, so those two predicates appear in both
files.

### Why step 4 exists

A gated fill is stored full size, so it can never co-allocate, so it can never
produce a reward. Without step 4 the predictor would drop into Regular Mode
once and wedge at its floor forever. The original design does not need this,
because it runs the compressor on every allocation whether or not it stores the
result, so the computed size keeps feeding its avoidable-miss test for free.
gem5's tags are not decoupled that way. `sample_interval` is this
reproduction's addition, and it is labelled as such everywhere it appears.

### Scoring

`runAndScore()` ([`acc.cc:31`](src/ehs/acc.cc:31)) runs the compressor and, if
`failed_penalty` is set, debits the GCP when the result comes back full size.
The published design needs no such penalty: it always pays compression cost, so
that cost is a constant that cancels, and incompressible data simply stops
earning rewards. `failed_penalty` defaults to 0, which is published behaviour.

`reward()` and `penalize()` ([`:91`](src/ehs/acc.cc:91),
[`:105`](src/ehs/acc.cc:105)) saturate at `gcp_max` and `gcp_min`, and count a
`modeSwitch` whenever `compressionEnabled()` flips.

---

## `ACCCache`: feeding the predictor

[`acc_cache.hh`](src/ehs/acc_cache.hh), [`.cc`](src/ehs/acc_cache.cc),
141 lines

The predictor lives in the compressor, but the compressor is called from a
non-virtual path in `BaseCache` and cannot see hits. `access()` is virtual, so
a thin `Cache` subclass can. That is the entire reason this class exists.

### Classifying hits

`access()` ([`acc_cache.cc:99`](src/ehs/acc_cache.cc:99)) calls
`Cache::access()` and then looks at the block on a hit:

```cpp
if (cblk->isCompressed()) {
    if (superblock->getNumValid() > 1)  acc->reward(currentReward());
    else                                acc->penalize(cblk->getDecompressionLatency());
}
```

Co-allocated means the block shares its superblock with another valid block, so
compression's extra capacity is what is holding it and the hit would otherwise
have been a miss. That earns the miss penalty as credit.

Alone means the block would have been resident anyway, and the access still
paid decompression latency for nothing. That gets debited exactly that latency,
read from the block itself.

Hits to uncompressed blocks say nothing about compression, so the GCP is left
alone.

### Pricing the reward

`currentReward()` ([`:63`](src/ehs/acc_cache.cc:63)) returns either the
configured constant or, when `reward_cycles < 0`, a measured value:

```
reward = max(round(avgMissPenalty in cycles) - hitCycles, 0)
hitCycles = sequentialAccess ? lookup + data : max(lookup, data)
```

The miss penalty is sampled in `recvTimingResp()`
([`:28`](src/ehs/acc_cache.cc:28)) as
`curTick() - target->recvTime + responseLatency`, accumulated over every
response, and reported as `avgMissPenaltyTicks` so any run can be audited.
Measuring it rather than fixing it matters in a sweep. The miss penalty is a
property of the machine (geometry, workload, queueing), so a constant
calibrated at one cell misprices every other one, and two rows would then
differ by their calibration instead of by the policy under test.

### The bracket pattern

`compress()` cannot take extra arguments, so per-fill facts reach the
compressor as sticky flags set around the base call
([`:45-59`](src/ehs/acc_cache.cc:45)):

```cpp
acc->setFillServesWrite(mshr->needsWritable());   // was this miss a store?
acc->setFillVaddr(vaddr, have_vaddr);             // for the software hint
Cache::recvTimingResp(pkt);                       // fill happens in here
acc->setFillServesWrite(false);                   // clear
acc->setFillVaddr(0, false);
```

The MSHR is the record of the outstanding miss and the base call destroys it,
so everything needed from it is read first. The virtual address comes from the
MSHR's target packet, not from the response, which carries none.

### Reporting evictions

`evictBlock()` ([`:89`](src/ehs/acc_cache.cc:89)) calls
`kagura->blockEvicted()` before delegating to `Cache::evictBlock()`. This is
the only place evictions are visible, and it is how Kagura's AIMD gets its
input. The configs set `kagura` on the D-cache only.

---

## `KaguraController`: the paper's policy

[`kagura_controller.hh`](src/ehs/kagura_controller.hh),
[`.cc`](src/ehs/kagura_controller.cc), 316 lines

An `IntermittentController` subclass. Same capacitor and checkpoint model, plus
five registers and a 2-bit counter:

| Register | Meaning |
|---|---|
| `R_mem` | memory operations committed so far this power cycle |
| `R_prev` | the previous cycle's count, corrected |
| `R_thres` | the threshold at which compression gets disabled |
| `R_evict` | blocks evicted since the decision point |
| `R_adjust` | the learned correction, `R_mem - R_prev` |
| `satCounter` | 2-bit confidence in the estimate |

ACC asks whether compression is making any gain. Kagura asks the question ACC
cannot: will this block live long enough to earn anything at all?

### Counting

`regProbeListeners()` ([`:58`](src/ehs/kagura_controller.cc:58)) connects to
the CPU's `RetiredLoads` and `RetiredStores` probe points. The registers sit
conceptually at the LSQ, and commit probes are the closest observation point
gem5 offers.

### The decision point

`memOpCommitted()` ([`:78`](src/ehs/kagura_controller.cc:78)) runs once per
committed memory op:

```cpp
const int64_t nRemain = int64_t(rPrev) - int64_t(rMem);
if (nRemain <= int64_t(rThres)) { ... regularMode = true; broadcastMode(true); }
```

`nRemain` is signed because a cycle can outrun its predecessor.
`broadcastMode()` ([`:71`](src/ehs/kagura_controller.cc:71)) calls
`setRegularMode(true)` on every compressor in the vector, the I-cache's
included. That is the point: Kagura's trigger is the power-cycle countdown, not
decompression evidence, so it can act on a cache ACC never gates.

Two optional vetoes can block the transition
([`:107-110`](src/ehs/kagura_controller.cc:107)): a confidence floor
(`rm_confidence_min`) and a perceptron over cycle-outcome history
(`rm_perceptron`). Both default off, which is published behaviour. Once a veto
fires, `gateBlocked` short-circuits the rest of the cycle, since `satCounter`
only moves at power-off and the verdict cannot change until then.

### At power-off

`powerOff()` ([`:147`](src/ehs/kagura_controller.cc:147)) computes
`R_adjust = R_mem - R_prev`, scores the estimate (within `closeness_frac` of
`R_prev` rewards the counter, otherwise it is punished), trains the perceptron
if it is enabled, then chains to `IntermittentController::powerOff()` for the
checkpoint itself.

### At reboot

`thawCpu()` ([`:206`](src/ehs/kagura_controller.cc:206)) runs the recovery
sequence:

1. `rPrev = rMem; rMem = 0`. `R_prev` is not checkpointed, it is rebuilt from
   the restored `R_mem`.
2. Apply `R_adjust` only while `satCounter <= 1`, so only while the counter
   says raw history has been estimating badly.
3. AIMD on `R_thres`: halve it if `rEvict > rThres / 2`, otherwise add
   `max(rThres / 10, 1)`. The additive step is floored at 1 so a threshold of 4
   can still move, and halving floors at 1 so the decision point stays
   reachable. A vetoed cycle skips the update entirely
   ([`:227`](src/ehs/kagura_controller.cc:227)), because `R_evict` measured no
   Regular Mode pressure and there is nothing to learn from. An optional cap
   (`thres_cap_frac`) clamps `R_thres` to a fraction of `R_prev`.
4. Recompute the perceptron output for the coming cycle. Its inputs only change
   at cycle boundaries, so this is the right place for it.
5. `broadcastMode(false)`, compression back on.

The AIMD comparison in step 3 is worth noting, because a whole sweep axis is
aimed at it. `R_evict` counts blocks and `R_thres` counts memory operations,
and those are not the same currency. `sweep.sh thres` pins the threshold with
`--no-aimd` so the curve AIMD is searching can be traced directly.

---

## Fixes outside `src/ehs/`

**`src/mem/cache/tags/compressed_tags.cc`.** `CompressedTags::tagsInit()`
overrides `BaseSetAssoc::tagsInit()` and never registers a tag extractor, so
`matchTag()` and `insert()` hit `assert(extractTag)` on a null `std::function`.
Compressed caches do not work in stock v25.1.0.1 without this. It is an
upstream gem5 bug, not something specific to this study.

**ARM 32-bit time64 syscalls.** `clock_gettime64` (403) and
`clock_getres_time64` (406), in `src/arch/arm/linux/` and
`src/sim/syscall_emul.hh`. A glibc built with 64-bit `time_t` issues these
instead of the 32-bit forms, so statically linked ARM binaries abort in SE mode
without them.

---

## Building and running

```bash
scons build/ARM/gem5.opt -j$(nproc)

sudo apt install -y gcc-arm-linux-gnueabi
bash workloads/build_arm.sh          # MiBench -> workloads/bin/
```

Workloads are built soft-float Thumb-2 (`-march=armv7-a -mthumb`, `gnueabi`
ABI) to stay close to the paper's FPU-less Cortex-M target. FP arguments travel
in integer registers and FP arithmetic becomes library calls, so a checkpoint
saves 68 B of architectural state instead of the 328 B a hard-float build
forces through a VFP/NEON register file.

```bash
./build/ARM/gem5.opt configs/kagura/stage_five_check.py \
    --cmd workloads/bin/qsort_small \
    --cwd workloads/mibench/automotive/qsort \
    --options input_small.dat
```

### The six stage configs

Each one adds exactly one thing to the one before it, and gets validated before
the next is written, so a result stays attached to the script that produced it.

| Stage | Adds |
|---|---|
| 1 `baseline_se.py` | MinorCPU + SRAM L1s + NVM. No compression, no power failures |
| 2 `stage_two_check.py` | Capacitor, power failures, checkpoint cost model |
| 3 `stage_three_check.py` | BDI on both L1s, always on, energy charged; ReRAM at Table I timings |
| 4 `stage_four_check.py` | ACC's GCP gating each cache |
| 5 `stage_five_check.py` | Kagura's end-of-cycle mode switching |
| 6 `stage_six_sweep.py` | Every parameter above as a command-line knob |

Stage 6 is a superset. A bare run reproduces the stage 5 operating point
exactly, and `--compression {none,bdi,acc,kagura}` picks any of the four
policies from one script with everything else held fixed.

### Sweeps and controls

```bash
bash configs/kagura/sweep.sh regress   # reproduce stage 5, gate on everything else
bash configs/kagura/sweep.sh size      # cache size x policy
bash configs/kagura/sweep.sh thres     # pinned N_thres, AIMD off
bash configs/kagura/sweep.sh attrib    # is the saving Kagura's, or ACC's blind spot?
```

Run these from the gem5 root. Each mode appends to `sweep/results.csv` and
leaves the full gem5 output under `sweep/<label>/`.

Two more scripts attack the study's own results instead of producing them.
`mEQUIV.sh` forces ACC permanently into Regular Mode (`--gcp-init 0
--sample-interval 0`) so every fill is stored full size, and asks whether
`CompressedTags` then behaves like a plain cache. It reaches its capacity by
halving the sets and doubling the associativity, so if it does not, every
margin measured against an uncompressed baseline carries an unknown
conflict-miss offset. `mSAMP.sh` sweeps `sample_interval` to bound how much of
any Kagura-over-ACC margin is really just the removal of the sampler described
above.

`ckptgate_report.py` and `wstream_report.py` read `stats.txt` directly, for the
two sweep modes whose questions the shared CSV schema cannot express. Both
hard-code the energy constants the sweep passed in, because `stats.txt` does
not record them.

---

## Deviations from the paper

The first thing to check against any number this model produces.

**ISA.** The paper compiles for ARMv7-M. gem5 has no M-profile model, so, as
the paper also does, Thumb-2 code runs on the A-profile model, in syscall
emulation rather than bare metal. Residual impurity: the toolchain's static
glibc objects are plain-ARM ARMv5.

**Checkpoint semantics.** NVSRAMCache halts at V_ckpt and squashes uncommitted
in-flight instructions, which then re-execute after restore. gem5 has no
mid-flight squash for external agents, so this drains instead, completing
in-flight work. Both land on a precise commit boundary. They differ by a few
instructions of position, and the drained LSQ plays the role of the
checkpointed store buffer.

**Predictor scope.** One GCP per cache rather than one global counter, because
gem5 binds a compressor to a single cache.

**ReRAM model.** Table I's parameter names (`tCK`/`tBURST`/`tRCD`/`tCL`/`tWTR`/
`tWR`/`tXAW`) are gem5 `DRAMInterface` parameters, and `NVMInterface` has none
of them. So the paper modeled ReRAM as a DRAM interface with slowed timings,
and this does the same. Unspecified timings inherit `DDR3_1600_8x8`, and
refresh is defanged with a very long `tREFI`.

**NVM write energy.** Table I publishes SRAM access and BDI
compress/decompress energies but no per-byte ReRAM write energy, since the
paper derives its figures from McPAT/CACTI at 45 nm. The value here is a
placeholder, and every checkpoint-energy number is linear in it, which is why
it gets swept rather than fixed.

**Sampling.** `sample_interval` is not part of the published ACC. See the `ACC`
section above and `mSAMP.sh`.

---

## Where the results are

This document covers the artifact. The measurements, the figures, and the
argument they support are in the EUROP report. `sweep/results.csv` and the two
report scripts are what that report is built from.
