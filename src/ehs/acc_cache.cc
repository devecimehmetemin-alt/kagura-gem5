 #include "ehs/acc_cache.hh"

#include <algorithm>

#include "base/logging.hh"
#include "ehs/acc.hh"
#include "ehs/kagura_controller.hh"
#include "mem/cache/mshr.hh"
#include "mem/cache/tags/super_blk.hh"

namespace gem5
{

ACCCache::ACCCache(const Params &p)
  : Cache(p),
    acc(dynamic_cast<compression::ACC *>(compressor)),
    kagura(p.kagura),
    rewardCycles(p.reward_cycles),
    autoReward(p.reward_cycles < 0),
    missPenaltyTicks(0),
    missPenaltySamples(0),
    accCacheStats(this)
{
    fatal_if(!acc, "%s: an ACCCache requires an ACC compressor", name());
}

void
ACCCache::recvTimingResp(PacketPtr pkt)
{
    // Both jobs below need the MSHR
    auto *mshr = dynamic_cast<MSHR *>(pkt->senderState);

    if (autoReward && !pkt->req->isUncacheable() && mshr) {
        if (const QueueEntry::Target *tgt = mshr->getTarget()) {
            // measure penalty and keep stats
            const Tick penalty = curTick() - tgt->recvTime +
                                 cyclesToTicks(responseLatency);
            missPenaltyTicks += penalty;
            missPenaltySamples++;
            accCacheStats.missPenaltyTicks += penalty;
            accCacheStats.missPenaltySamples++;
        }
    }

    // Check if this miss was caused by a store
    if (mshr)
        acc->setFillServesWrite(mshr->needsWritable());

    // The address signal for the software compressibility hint
    const QueueEntry::Target *hint_tgt = mshr ? mshr->getTarget() : nullptr;
    const bool have_vaddr = hint_tgt && hint_tgt->pkt &&
                            hint_tgt->pkt->req->hasVaddr();
    acc->setFillVaddr(have_vaddr ? hint_tgt->pkt->req->getVaddr() : 0,
                      have_vaddr);

    Cache::recvTimingResp(pkt);

    acc->setFillServesWrite(false);
    acc->setFillVaddr(0, false);
}

int64_t
ACCCache::currentReward() const
{
    if (!autoReward)
        return rewardCycles;

    // Nothing has missed yet, so there is no penalty for
    //compression to have avoided
    if (missPenaltySamples == 0)
        return 0;

    const Tick avgPenalty = missPenaltyTicks / missPenaltySamples;

    // Assess how many extra cycles a miss causes
    const Tick perCycle = cyclesToTicks(Cycles(1));
    const int64_t penaltyCycles =
        int64_t((avgPenalty + perCycle / 2) / perCycle);

    // Assess how many cycles is to hit
    const int64_t hitCycles = sequentialAccess
        ? int64_t(lookupLatency) + int64_t(dataLatency)
        : std::max(int64_t(lookupLatency), int64_t(dataLatency));

    return std::max<int64_t>(penaltyCycles - hitCycles, 0);
}

PacketPtr
ACCCache::evictBlock(CacheBlk *blk)
{
    // Report before the eviction happens
    if (kagura)
        kagura->blockEvicted();

    return Cache::evictBlock(blk);
}

bool
ACCCache::access(PacketPtr pkt, CacheBlk *&blk, Cycles &lat,
                 PacketList &writebacks)
{
    const bool hit = Cache::access(pkt, blk, lat, writebacks);

    if (hit && blk) {
    //
        if (const auto *cblk = dynamic_cast<CompressionBlk *>(blk)) {
            if (cblk->isCompressed()) { //checks if compressed block
                const auto *superblock =
                    static_cast<const SuperBlk *>(cblk->getSectorBlock());

                if (superblock->getNumValid() > 1) {
                    // two blocks, reward
                    acc->reward(currentReward());
                } else {
                    // 1 block, penalize
                    acc->penalize(cblk->getDecompressionLatency());
                }
            }
        }
    }

    return hit;
}

ACCCache::ACCCacheStats::ACCCacheStats(statistics::Group *parent)
  : statistics::Group(parent),
    ADD_STAT(missPenaltyTicks, statistics::units::Tick::get(),
             "Miss penalty sampled from responses (0 unless the GCP reward "
             "is measured rather than given)"),
    ADD_STAT(missPenaltySamples, statistics::units::Count::get(),
             "Responses the miss penalty was sampled over"),
    ADD_STAT(avgMissPenaltyTicks, statistics::units::Tick::get(),
             "Measured miss penalty the GCP was rewarded with; the "
             "calibration the run used, recorded so a swept row can be "
             "audited")
{
    avgMissPenaltyTicks = missPenaltyTicks / missPenaltySamples;
    avgMissPenaltyTicks.precision(1);
}

} // namespace gem5
