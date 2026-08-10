#ifndef __EHS_INTERMITTENT_CONTROLLER_SIM_OBJECT__
#define __EHS_INTERMITTENT_CONTROLLER_SIM_OBJECT__

#include <cstdint>
#include <fstream>
#include <string>
#include <vector>

#include "base/statistics.hh"
#include "params/IntermittentController.hh"
#include "sim/clocked_object.hh"

namespace gem5
{
class BaseCPU;
class BaseTags;

namespace memory
{
class NvmMemCtrl;
} // namespace memory

namespace compression
{
class Base;
class EnergyCompressor;
} // namespace compression

    class IntermittentController : public ClockedObject
    {
        private:
            double capacitance;        // Farads
            double maximumVoltage;     // fully-charged voltage
            double maximumEnergy;      // energy at maximumVoltage (Joules)
            double capacitorVoltage;   // current voltage
            double storedEnergy;       // current stored energy (Joules)
            Tick   lastUpdateTick;     // tick of previous capacitor update

            // Synthetic square-wave harvest source (the default).
            double harvestOnPower;     // Watts while ON
            double harvestPeriod;      // seconds per ON+OFF cycle
            double dutyCycle;          // fraction of the period that is ON

            // File-based harvest trace, sample-and-hold
            std::vector<double> traceTimes;   // seconds, strictly increasing
            std::vector<double> tracePowers;  // Watts
            double traceEnd;      // where the last sample's hold interval ends
            bool   traceLoop;     // wrap when time outruns the trace
            size_t traceCursor;   // monotonic lookup cursor

            double turnOnVoltage;   // v_on
            double turnOffVoltage;  // v_off

        protected:
            // Protected rather than private: KaguraController hooks
            // the CPU's retired-load/store probes and broadcasts its mode to
            // the compressors.
            BaseCPU *cpu;

        private:
            bool powered;           // current power state
            Tick poweredOffAt;      // tick of the last power failure

            double  staticPower;     // W, leakage floor while powered
            double  energyPerInst;   // J per committed instruction
            Counter lastInstCount;   // totalInsts() at the previous sample

            // JIT checkpoint cost. Every dirty block in these tag stores has
            // to reach NVM before the capacitor runs out, so the checkpoint
            // is sized in bytes and then priced per byte.
            std::vector<BaseTags *> ckptTags;
            double   energyPerByteNvm;  // J per byte written to NVM
            double   timePerByteNvm;    // s per byte written to NVM
            unsigned ckptRegBytes;      // bytes

            // In a real system checkpointing takes time but in gem5
            // it doesn't. To account for this, we assign timePerByteNVM
            // to calculate time how much time this will take. The system
            // can power on only if this time is reached.
            bool ckptTimeEnabled;
            Tick ckptReadyTick;

            // Measurement-only compressor run over the dirty blocks at each
            // checkpoint. It records what the dirty set would compress to.
            // Nothing from this reaches capacitor energy model
            compression::Base *ckptProbeCompressor;

            // Golden model vector dump for an implementation in hardware.
            // Nothing from here reaches capacitor model.
            std::ofstream vectorDump;

        protected:
            // Compression energy. The compressors accumulate the energy they
            // spend. The controller samples that total once per update and
            // drains the difference from the capacitor, so compressing costs
            // the program charge it could have run on.
            std::vector<compression::EnergyCompressor *> compressors;

        private:
            double lastCompressionEnergy;  // total at the previous sample (J)

            // Main-memory traffic energy. The bytes cache misses move to and
            // from NVM are the largest term in this system and the
            // one compression exists to reduce
            memory::NvmMemCtrl *nvm;
            double   energyPerByteNvmRead;   // J per byte filled from NVM
            double   energyPerByteNvmWrite;  // J per byte written back to NVM
            uint64_t lastNvmReadBytes;       // totals at the previous sample
            uint64_t lastNvmWriteBytes;

            // switch for whether NVM traffic actually drains
            // the capacitor, or is only counted.
            bool nvmEnergyToCapacitor;

            EventFunctionWrapper updateEvent;

            // Stats
            struct IntermittentControllerStats : public statistics::Group
            {
                IntermittentControllerStats(statistics::Group *parent);
                statistics::Scalar numPowerFailures;
                statistics::Scalar numRestores;
                statistics::Scalar ticksPoweredOff;
                statistics::Scalar checkpointBytes;
                statistics::Scalar checkpointDirtyBytes;
                statistics::Scalar checkpointEnergy;
                statistics::Scalar checkpointTicks;
                statistics::Scalar ckptStallTicks;
                statistics::Scalar compressionEnergy;
                statistics::Scalar staticEnergy;
                statistics::Scalar dynamicEnergy;

                // NVM traffic
                statistics::Scalar nvmReadBytes;
                statistics::Scalar nvmWriteBytes;
                statistics::Scalar nvmReadEnergy;
                statistics::Scalar nvmWriteEnergy;
                statistics::Formula nvmEnergy;
                statistics::Scalar nvmBytesOffPeriod;
                statistics::Scalar nvmEnergyOffPeriod;

                // Checkpoint probe
                statistics::Scalar ckptProbeBlocks;
                statistics::Scalar ckptProbeUncompBytes;
                statistics::Scalar ckptProbeCompBytes;
                statistics::Scalar ckptProbeZeroBlocks;
                statistics::Scalar ckptProbeGe2x;
                statistics::Scalar ckptProbeGe4x;

                // Where the energy went. Every term the capacitor pays for,
                // and each one's share, so the ceiling on any policy that
                // only touches one term can be read off directly.
                statistics::Formula budgetEnergy;
                statistics::Formula staticEnergyFrac;
                statistics::Formula dynamicEnergyFrac;
                statistics::Formula compressionEnergyFrac;
                statistics::Formula nvmEnergyFrac;
                statistics::Formula checkpointEnergyFrac;
            } stats;

            void updateCapacitor();
            void loadTrace(const std::string &path, double scale);
            double getHarvestPower(Tick when);
            double getConsumePower(double dt);

            // Bytes of dirty cache data a checkpoint must save.
            uint64_t countDirtyBytes();

            // Runs the probe compressor over every dirty block and records
            // the achieved sizes.
            void probeDirtyBlocks();

            // CPU power control
            void freezeCpu();

        protected:
            // power-on is where Kagura runs its recovery
            // sequence (restore R_prev from R_mem, apply R_adjust, AIMD-tune
            // R_thres), so the subclass overrides and extends it.
            virtual void thawCpu();

        public:
            IntermittentController(const IntermittentControllerParams &params);
            void startup() override;

            // Exported to Python (cxx_exports). Called by the config's
            // power-cycle loop after m5.drain(), suspends the CPU at the
            // drained (checkpoint) boundary. Checkpoint is also where
            // Kagura records its parameters
            virtual void powerOff();
    };

} // namespace gem5

#endif
