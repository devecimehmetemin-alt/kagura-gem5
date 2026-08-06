#include "ehs/intermittent_minor_cpu.hh"

#include "base/logging.hh"
#include "cpu/minor/pipeline.hh"
#include "sim/system.hh"

namespace gem5
{

    IntermittentMinorCPU::IntermittentMinorCPU(
            const IntermittentMinorCPUParams &params) :
        MinorCPU(params)
    {
    }

    void
    IntermittentMinorCPU::drainResume()
    {
        // Same body as MinorCPU::drainResume() without the per thread
        // wakeup() loop. Threads suspended by the IntermittentController
        // must stay suspended until it calls activateContext() at power-on.
        pipeline->resetLastStopped();

        if (switchedOut())
            return;

        if (!system->isTimingMode()) {
            fatal("The Minor CPU requires the memory system to be in "
                  "'timing' mode.\n");
        }

        pipeline->drainResume();
        schedulePowerGatingEvent();
    }

} // namespace gem5
