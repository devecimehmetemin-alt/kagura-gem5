#ifndef __EHS_NVM_MEM_CTRL_HH__
#define __EHS_NVM_MEM_CTRL_HH__

#include <cstdint>

#include "mem/mem_ctrl.hh"
#include "params/NvmMemCtrl.hh"

namespace gem5
{
// MemCtrl lives one namespace deeper than the rest of the SimObjects
namespace memory
{

/**
 * A MemCtrl that lets the power model read how much traffic it has served.
 *
 * The energy an intermittent system spends on main memory is not a detail: on
 * this machine, cache misses move about 215 MB to and from ReRAM over a run of
 * qsort, which at any plausible per-byte figure is the largest energy term.
 * It is also the term compression exists to reduce, so a model
 * that charges for compression but not for the traffic compression avoids is
 * biased against compression by construction.
 */
class NvmMemCtrl : public MemCtrl
{
  public:
    typedef NvmMemCtrlParams Params;
    NvmMemCtrl(const Params &p);

    /** Bytes read from NVM (cache fills) since the run began. */
    uint64_t bytesRead() const;

    /** Bytes written to NVM (dirty writebacks during execution) so far. */
    uint64_t bytesWritten() const;
};

} // namespace memory
} // namespace gem5

#endif //__EHS_NVM_MEM_CTRL_HH__
