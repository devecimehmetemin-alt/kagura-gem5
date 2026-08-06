#include "ehs/nvm_mem_ctrl.hh"

namespace gem5
{
namespace memory
{

NvmMemCtrl::NvmMemCtrl(const Params &p)
  : MemCtrl(p)
{
}

uint64_t
NvmMemCtrl::bytesRead() const
{
    return uint64_t(stats.bytesReadSys.value());
}

uint64_t
NvmMemCtrl::bytesWritten() const
{
    return uint64_t(stats.bytesWrittenSys.value());
}

} // namespace memory
} // namespace gem5
