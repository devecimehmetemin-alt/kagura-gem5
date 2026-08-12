#include "ehs/intermittent_controller.hh"

#include <algorithm>
#include <cmath>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <sstream>
#include "cpu/base.hh"
#include "cpu/thread_context.hh"
#include "base/logging.hh"
#include "base/trace.hh"
#include "debug/IntermittentController.hh"
#include "ehs/energy_compressor.hh"
#include "ehs/nvm_mem_ctrl.hh"
#include "mem/cache/cache_blk.hh"
#include "mem/cache/compressors/base.hh"
#include "mem/cache/tags/base.hh"
#include "mem/cache/tags/super_blk.hh"
#include "sim/core.hh"
#include "sim/sim_exit.hh"

namespace gem5
{

    IntermittentController::IntermittentController(const IntermittentControllerParams &params) : ClockedObject(params),
        capacitance(params.capacitance),
        maximumVoltage(params.v_max),
        maximumEnergy(0.5 * params.capacitance * params.v_max * params.v_max),
        capacitorVoltage(params.v_max),
        storedEnergy(0.5 * params.capacitance * params.v_max * params.v_max),
        lastUpdateTick(0),
        harvestOnPower(params.p_harvest),
        harvestPeriod(params.harvest_period),
        dutyCycle(params.duty_cycle),
        traceEnd(0.0),
        traceLoop(params.trace_loop),
        traceCursor(0),
        turnOnVoltage(params.v_on),
        turnOffVoltage(params.v_off),
        cpu(params.cpu),
        powered(true),          // system boots powered/full
        poweredOffAt(0),
        staticPower(params.p_static),
        energyPerInst(params.e_per_inst),
        lastInstCount(0),
        ckptTags(params.ckpt_tags),
        energyPerByteNvm(params.e_per_byte_nvm),
        timePerByteNvm(params.t_per_byte_nvm),
        ckptRegBytes(params.ckpt_reg_bytes),
        ckptTimeEnabled(params.ckpt_time),
        ckptReadyTick(0),
        ckptProbeCompressor(params.ckpt_probe_compressor),
        compressors(params.compressors),
        lastCompressionEnergy(0.0),
        nvm(params.nvm),
        energyPerByteNvmRead(params.e_per_byte_nvm_read),
        energyPerByteNvmWrite(params.e_per_byte_nvm_write),
        lastNvmReadBytes(0),
        lastNvmWriteBytes(0),
        nvmEnergyToCapacitor(params.nvm_energy_to_capacitor),
        updateEvent([this] { updateCapacitor(); }, name() + ".updateEvent"),
        stats(this)
    {
        if (!params.trace_file.empty())
            loadTrace(params.trace_file, params.trace_scale);

        if (!params.vector_dump_path.empty()) {
            vectorDump.open(params.vector_dump_path);
            vectorDump <<
                "# per dirty block: bytes (hex, address order) | size_bits"
                " | comp_bytes\n"
                "# chunk k = little-endian uint64 of bytes[8k..8k+7]\n";
        }

        std::cout << "Intermittent Controller created" << std::endl;
    }

    void
    IntermittentController::startup()
    {
        // 1 us steps (1000 cycles at 1 GHz): the capacitor time constant is
        // ~ms at the calibrated mW power levels
        schedule(updateEvent, clockEdge(Cycles(1000))); // first tick
    }

    void
    IntermittentController::updateCapacitor()
    {
        double dt = (curTick() - lastUpdateTick) / double(sim_clock::Frequency);

        double p_in = getHarvestPower(curTick());   // from power-trace reader
        double p_out = getConsumePower(dt);         // activity-based CPU power

        lastUpdateTick = curTick();

        storedEnergy += (p_in - p_out) * dt;

        // Clamp capacitor energy at max and 0
        if (storedEnergy > maximumEnergy)
            storedEnergy = maximumEnergy;
        if (storedEnergy < 0.0)
            storedEnergy = 0.0;

        capacitorVoltage = std::sqrt(2 * storedEnergy / capacitance);

        if (powered && capacitorVoltage <= turnOffVoltage) {
            powered = false;
            freezeCpu();                     // power failure — stop the CPU
            DPRINTF(IntermittentController,
                    "POWER FAILURE  V=%.4f <= v_off=%.4f\n",
                    capacitorVoltage, turnOffVoltage);
        } else if (!powered && capacitorVoltage >= turnOnVoltage &&
                   curTick() >= ckptReadyTick) {
            powered = true;
            thawCpu();                       // recovered — resume the CPU
            DPRINTF(IntermittentController,
                    "POWER RESTORED V=%.4f >= v_on=%.4f\n",
                    capacitorVoltage, turnOnVoltage);
        }

        DPRINTF(IntermittentController,
                "t=%.9f s  P_in=%.3f W  P_out=%.3f W  E=%.3e J  V=%.4f V\n",
                curTick() / double(sim_clock::Frequency),
                p_in, p_out, storedEnergy, capacitorVoltage);

        schedule(updateEvent, clockEdge(Cycles(1000)));  // reschedule self
    }

    // Parse a harvest trace
    void
    IntermittentController::loadTrace(const std::string &path, double scale)
    {
        std::ifstream in(path);
        if (!in)
            fatal("IntermittentController: cannot open trace file '%s'", path);

        std::string line;
        unsigned lineno = 0;
        while (std::getline(in, line)) {
            lineno++;
            const auto hash = line.find('#');
            if (hash != std::string::npos)
                line.erase(hash);

            std::istringstream fields(line);
            double t, p;
            if (!(fields >> t))
                continue;                    // blank or comment-only line
            if (!(fields >> p))
                fatal("%s:%u: expected 'time_s power_W'", path, lineno);
            if (p < 0)
                fatal("%s:%u: negative power %f", path, lineno, p);
            if (!traceTimes.empty() && t <= traceTimes.back())
                fatal("%s:%u: times must be strictly increasing", path,
                      lineno);

            traceTimes.push_back(t);
            tracePowers.push_back(p * scale);
        }

        if (traceTimes.size() < 2)
            fatal("IntermittentController: trace '%s' has %d samples, "
                  "need at least 2", path, traceTimes.size());

        traceEnd = traceTimes.back() +
                   (traceTimes.back() - traceTimes[traceTimes.size() - 2]);

        double joules = 0.0;
        for (size_t i = 0; i < tracePowers.size(); i++) {
            const double next =
                (i + 1 < traceTimes.size()) ? traceTimes[i + 1] : traceEnd;
            joules += tracePowers[i] * (next - traceTimes[i]);
        }
        inform("harvest trace '%s': %d samples, %.3f s, mean %.3e W%s",
               path, traceTimes.size(), traceEnd, joules / traceEnd,
               traceLoop ? ", looping" : ", holds last sample at end");
    }

    // Harvest power at a point in time.
    // the file-based trace when one is loaded,
    // otherwise the synthetic square wave
    double
    IntermittentController::getHarvestPower(Tick when)
    {
        double t = when / double(sim_clock::Frequency);   // ticks -> seconds

        if (traceTimes.empty()) {
            double phase = std::fmod(t, harvestPeriod);   // position in cycle
            return (phase < dutyCycle * harvestPeriod) ? harvestOnPower : 0.0;
        }

        if (t >= traceEnd) {
            if (!traceLoop)
                return tracePowers.back();
            t = std::fmod(t, traceEnd);
        }

        // Sample-and-hold lookup. wrap case considered
        if (t < traceTimes[traceCursor])
            traceCursor = 0;
        while (traceCursor + 1 < traceTimes.size() &&
               traceTimes[traceCursor + 1] <= t) {
            traceCursor++;
        }

        // Before the first timestamp this returns the first sample
        return tracePowers[traceCursor];
    }

    // Activity-based CPU power: leakage floor, plus dynamic energy from the
    // instructions committed since the previous sample, plus the energy the
    // compressors spent over the same interval. Every call advances the
    // baselines, so this must be called exactly once per capacitor update.
    double
    IntermittentController::getConsumePower(double dt)
    {
        double compressionEnergy = 0.0;
        for (auto *compressor : compressors)
            compressionEnergy += compressor->getEnergy();

        double compressionDelta = compressionEnergy - lastCompressionEnergy;
        lastCompressionEnergy = compressionEnergy;
        stats.compressionEnergy += compressionDelta;

        // Main-memory traffic energy
        double nvmDelta = 0.0;
        if (nvm) {
            const uint64_t readBytes = nvm->bytesRead();
            const uint64_t writeBytes = nvm->bytesWritten();
            const uint64_t readDelta = readBytes - lastNvmReadBytes;
            const uint64_t writeDelta = writeBytes - lastNvmWriteBytes;
            lastNvmReadBytes = readBytes;
            lastNvmWriteBytes = writeBytes;

            const double readEnergy = readDelta * energyPerByteNvmRead;
            const double writeEnergy = writeDelta * energyPerByteNvmWrite;

            stats.nvmReadBytes += readDelta;
            stats.nvmWriteBytes += writeDelta;
            stats.nvmReadEnergy += readEnergy;
            stats.nvmWriteEnergy += writeEnergy;

            if (!powered) {
                stats.nvmBytesOffPeriod += readDelta + writeDelta;
                stats.nvmEnergyOffPeriod += readEnergy + writeEnergy;
            }

            if (nvmEnergyToCapacitor)
                nvmDelta = readEnergy + writeEnergy;
        }

        if (!powered)
            return 0.0;                      // core is dark

        Counter now = cpu->totalInsts();
        Counter delta = now - lastInstCount;
        lastInstCount = now;

        double p_dyn = (dt > 0) ? (delta * energyPerInst / dt) : 0.0;
        double p_comp = (dt > 0) ? (compressionDelta / dt) : 0.0;
        double p_nvm = (dt > 0) ? (nvmDelta / dt) : 0.0;

        // Same two terms as energies, so the budget can be broken down by
        // where it went. Only accrued while powered, which is why they sit
        // after the guard.
        stats.staticEnergy += staticPower * dt;
        stats.dynamicEnergy += delta * energyPerInst;

        return staticPower + p_dyn + p_comp + p_nvm;
    }

    // Power failure: the CPU cannot be suspended asynchronously,
    // so freezing is a two-step handshake with the config:
    // exit the simulation loop here, then the config drains the system
    // (completing all in-flight work), calls powerOff() to price and take the
    // JIT checkpoint at that clean boundary, and writes back and
    // invalidates the volatile caches.
    void
    IntermittentController::freezeCpu()
    {
        exitSimLoop("power failure");
    }

    // Size of the volatile state a checkpoint has to push to NVM. Walks the
    // tag stores and totals the dirty blocks. the I-cache is read-only, so in
    // practice only the D-cache contributes.
    uint64_t
    IntermittentController::countDirtyBytes()
    {
        const uint64_t lineBytes = cpu->cacheLineSize();
        uint64_t bytes = 0;

        for (auto *tags : ckptTags) {
            tags->forEachBlk([&bytes, lineBytes](CacheBlk &blk) {
                if (!blk.isSet(CacheBlk::DirtyBit))
                    return;

                if (auto *cblk = dynamic_cast<CompressionBlk *>(&blk)) {
                    bytes += (cblk->getSizeBits() + 7) / 8;
                } else {
                    bytes += lineBytes;
                }
            });
        }

        return bytes;
    }

    // The gate for checkpoint-time compression.
    // Answers the question what would the dirty set  compress to
    // if the checkpoint compressed it? Runs the probe compressor
    // over every dirty block and records the achieved sizes.
    void
    IntermittentController::probeDirtyBlocks()
    {
        const uint64_t lineBytes = cpu->cacheLineSize();

        for (auto *tags : ckptTags) {
            tags->forEachBlk([this, lineBytes](CacheBlk &blk) {
                if (!blk.isSet(CacheBlk::DirtyBit))
                    return;

                const uint8_t *data = blk.data;
                bool zero = true;
                for (uint64_t i = 0; i < lineBytes; i++) {
                    if (data[i] != 0) {
                        zero = false;
                        break;
                    }
                }

                Cycles compLat, decompLat;
                auto comp = ckptProbeCompressor->compress(
                    reinterpret_cast<const uint64_t *>(data),
                    compLat, decompLat);
                const std::size_t bits = comp->getSizeBits();

                const uint64_t compBytes = std::max<uint64_t>(
                    1, std::min<uint64_t>((bits + 7) / 8, lineBytes));

                if (vectorDump.is_open()) {
                    for (uint64_t i = 0; i < lineBytes; i++)
                        vectorDump << std::setfill('0') << std::setw(2)
                                   << std::hex << (unsigned)data[i];
                    vectorDump << std::dec << ' ' << bits << ' '
                               << compBytes << '\n';
                }

                stats.ckptProbeBlocks++;
                stats.ckptProbeUncompBytes += lineBytes;
                stats.ckptProbeCompBytes += compBytes;
                if (zero)
                    stats.ckptProbeZeroBlocks++;
                if (compBytes * 2 <= lineBytes)
                    stats.ckptProbeGe2x++;
                if (compBytes * 4 <= lineBytes)
                    stats.ckptProbeGe4x++;
            });
        }

        if (vectorDump.is_open())
            vectorDump.flush();
    }

    // Runs at the the JIT checkpoint point: the config
    // calls this after m5.drain() and *before* m5.memWriteback()
    void
    IntermittentController::powerOff()
    {
        // Dirty cache blocks plus the architectural state save energy
        if (ckptProbeCompressor)
            probeDirtyBlocks();

        uint64_t dirtyBytes = countDirtyBytes();
        uint64_t bytes = dirtyBytes + ckptRegBytes;
        double energy = bytes * energyPerByteNvm;

        // The whole purpose of v_off is to leave enough charge to finish the
        // checkpoint. If it does not, v_off is set too low for this checkpoint
        // size the model would silently lose state, hence a warning.
        if (energy > storedEnergy) {
            warn("Checkpoint of %llu B needs %.3e J but only %.3e J remain: "
                 "v_off is too low to complete it.\n",
                 bytes, energy, storedEnergy);
        }

        storedEnergy = std::max(0.0, storedEnergy - energy);
        capacitorVoltage = std::sqrt(2 * storedEnergy / capacitance);

        stats.checkpointBytes += bytes;
        stats.checkpointDirtyBytes += dirtyBytes;
        stats.checkpointEnergy += energy;

        //Calculate how long checkpointing takes
        const Tick ckptTicks =
            Tick(bytes * timePerByteNvm * sim_clock::Frequency);
        stats.checkpointTicks += ckptTicks;
        if (ckptTimeEnabled) {
            ckptReadyTick = curTick() + ckptTicks;
            stats.ckptStallTicks += ckptTicks;
        }

        DPRINTF(IntermittentController,
                "CHECKPOINT     %llu B  E=%.3e J  V=%.4f V\n",
                bytes, energy, capacitorVoltage);

        // Suspend through the thread context, not cpu->suspendContext().
        // Only ThreadContext::suspend() sets the thread's status to
        // Suspended; calling the CPU directly stops execution but leaves the
        // status reading Active. TimingSimpleCPU::drainResume() tests that
        // status, so the core would wake itself on the next m5.simulate()
        // with the capacitor still flat and run on while the model believed
        // it was dark.
        cpu->getContext(0)->suspend();
        poweredOffAt = curTick();
        stats.numPowerFailures++;
    }

    void
    IntermittentController::thawCpu()
    {
        // Paired with the suspend() in powerOff(): activate() sets the
        // status back to Active and calls the CPU's activateContext().
        cpu->getContext(0)->activate();
        stats.numRestores++;
        stats.ticksPoweredOff += curTick() - poweredOffAt;
    }

    IntermittentController::IntermittentControllerStats::
    IntermittentControllerStats(statistics::Group *parent) :
        statistics::Group(parent),
        ADD_STAT(numPowerFailures, statistics::units::Count::get(),
                 "Power failures (JIT checkpoints taken)"),
        ADD_STAT(numRestores, statistics::units::Count::get(),
                 "Power restores (resumes from checkpoint)"),
        ADD_STAT(ticksPoweredOff, statistics::units::Tick::get(),
                 "Total ticks spent powered off"),
        ADD_STAT(checkpointBytes, statistics::units::Byte::get(),
                 "Total bytes saved by JIT checkpoints "
                 "(dirty cache blocks + architectural state)"),
        ADD_STAT(checkpointDirtyBytes, statistics::units::Byte::get(),
                 "Of those, the dirty cache blocks alone -- the only part "
                 "cache compression can shrink"),
        ADD_STAT(checkpointEnergy, statistics::units::Joule::get(),
                 "Total energy spent writing checkpoints to NVM"),
        ADD_STAT(checkpointTicks, statistics::units::Tick::get(),
                 "Ticks a real NVM would have spent writing the checkpoints "
                 "(not simulated unless ckpt_time is set)"),
        ADD_STAT(ckptStallTicks, statistics::units::Tick::get(),
                 "Of those, ticks actually charged as simulated time by "
                 "delaying the restore (0 unless ckpt_time is set); the gap "
                 "to checkpointTicks is what the model forgives"),
        ADD_STAT(compressionEnergy, statistics::units::Joule::get(),
                 "Total energy the compressors drew from the capacitor"),
        ADD_STAT(staticEnergy, statistics::units::Joule::get(),
                 "Leakage energy: p_static integrated over the time the core "
                 "was powered. Lumped for the whole system, so the cache's "
                 "own share of it is not separable"),
        ADD_STAT(dynamicEnergy, statistics::units::Joule::get(),
                 "Switching energy: committed instructions x e_per_inst. "
                 "Also lumped -- covers the core, the register file and the "
                 "SRAM accesses together"),
        ADD_STAT(nvmReadBytes, statistics::units::Byte::get(),
                 "Bytes read from NVM on cache fills"),
        ADD_STAT(nvmWriteBytes, statistics::units::Byte::get(),
                 "Bytes written to NVM on dirty writebacks (execution only; "
                 "the checkpoint's writeback is functional and is priced "
                 "separately as checkpointEnergy)"),
        ADD_STAT(nvmReadEnergy, statistics::units::Joule::get(),
                 "Energy spent reading NVM -- the term cache compression "
                 "exists to reduce, and the largest in this system"),
        ADD_STAT(nvmWriteEnergy, statistics::units::Joule::get(),
                 "Energy spent writing back to NVM during execution"),
        ADD_STAT(nvmEnergy, statistics::units::Joule::get(),
                 "Total main-memory traffic energy; disjoint from "
                 "checkpointEnergy, so the two may be added"),
        ADD_STAT(nvmBytesOffPeriod, statistics::units::Byte::get(),
                 "Of the NVM bytes above, those moved after the power failure "
                 "while the core was dark (in-flight work completing during "
                 "the drain)"),
        ADD_STAT(nvmEnergyOffPeriod, statistics::units::Joule::get(),
                 "Their energy: counted in nvmEnergy but never drawn from the "
                 "capacitor, so this is how much the ledger over-reports. "
                 "Per power failure, so it scales with the failure count"),
        ADD_STAT(ckptProbeBlocks, statistics::units::Count::get(),
                 "Dirty blocks probed by the checkpoint-compression gate"),
        ADD_STAT(ckptProbeUncompBytes, statistics::units::Byte::get(),
                 "Uncompressed bytes of those blocks; must equal "
                 "checkpointDirtyBytes under an uncompressed cache"),
        ADD_STAT(ckptProbeCompBytes, statistics::units::Byte::get(),
                 "What the probe compressor achieved on them (verbatim "
                 "fallback applied); never charged, never stored"),
        ADD_STAT(ckptProbeZeroBlocks, statistics::units::Count::get(),
                 "Of the probed blocks, how many were all-zero -- the share "
                 "a bare zero-line detector would capture"),
        ADD_STAT(ckptProbeGe2x, statistics::units::Count::get(),
                 "Probed blocks that compressed to half the line or better"),
        ADD_STAT(ckptProbeGe4x, statistics::units::Count::get(),
                 "Probed blocks that compressed to a quarter or better"),
        ADD_STAT(budgetEnergy, statistics::units::Joule::get(),
                 "Every energy term the capacitor pays for, added up: "
                 "leakage + switching + compression + NVM traffic + "
                 "checkpoints"),
        ADD_STAT(staticEnergyFrac, statistics::units::Ratio::get(),
                 "Leakage as a share of the budget"),
        ADD_STAT(dynamicEnergyFrac, statistics::units::Ratio::get(),
                 "Switching as a share of the budget"),
        ADD_STAT(compressionEnergyFrac, statistics::units::Ratio::get(),
                 "Compression as a share of the budget. This is the ceiling "
                 "on any policy that only schedules compression: even a "
                 "perfect one that never compressed a wasted block could not "
                 "save more than this"),
        ADD_STAT(nvmEnergyFrac, statistics::units::Ratio::get(),
                 "Main-memory traffic as a share of the budget. This is what "
                 "compression can attack indirectly by avoiding misses, and "
                 "the ceiling on zero-cost compression"),
        ADD_STAT(checkpointEnergyFrac, statistics::units::Ratio::get(),
                 "Checkpoints as a share of the budget")
    {
        // Checkpoints cost sub-uJ; the default six-decimal stat formatting
        // would print every total as 0.000000 J.
        checkpointEnergy.precision(12);
        compressionEnergy.precision(12);
        staticEnergy.precision(12);
        dynamicEnergy.precision(12);
        nvmReadEnergy.precision(12);
        nvmWriteEnergy.precision(12);
        nvmEnergy.precision(12);
        nvmEnergyOffPeriod.precision(12);
        budgetEnergy.precision(12);

        nvmEnergy = nvmReadEnergy + nvmWriteEnergy;

        budgetEnergy = staticEnergy + dynamicEnergy + compressionEnergy +
                       nvmReadEnergy + nvmWriteEnergy + checkpointEnergy;

        staticEnergyFrac = staticEnergy / budgetEnergy;
        dynamicEnergyFrac = dynamicEnergy / budgetEnergy;
        compressionEnergyFrac = compressionEnergy / budgetEnergy;
        nvmEnergyFrac = (nvmReadEnergy + nvmWriteEnergy) / budgetEnergy;
        checkpointEnergyFrac = checkpointEnergy / budgetEnergy;

        staticEnergyFrac.precision(6);
        dynamicEnergyFrac.precision(6);
        compressionEnergyFrac.precision(6);
        nvmEnergyFrac.precision(6);
        checkpointEnergyFrac.precision(6);
    }

} // namespace gem5
