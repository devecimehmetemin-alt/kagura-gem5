#ifndef __EHS_INTERMITTENT_MINOR_CPU_HH__
#define __EHS_INTERMITTENT_MINOR_CPU_HH__

#include "cpu/minor/cpu.hh"
#include "params/IntermittentMinorCPU.hh"

namespace gem5
{

    // MinorCPU whose drainResume() leaves suspended threads suspended.
    //
    // The JIT-checkpoint drains the simulation at a power failure,
    // suspends the thread, and resumes the event loop so the capacitor can
    // recharge as off. Stock MinorCPU::drainResume() calls wakeup() hence
    // powers up the core the moment a sim instant occurs. We want
    // IntermittentController to do this. Hence this subclass overrides
    // drainResume()
    class IntermittentMinorCPU : public MinorCPU
    {
      public:
        IntermittentMinorCPU(const IntermittentMinorCPUParams &params);
        void drainResume() override;
    };

} // namespace gem5

#endif
