#ifndef __EHS_ACC_CACHE_HH__
#define __EHS_ACC_CACHE_HH__

#include <cstdint>

#include "base/statistics.hh"
#include "base/types.hh"
#include "mem/cache/cache.hh"
#include "params/ACCCache.hh"

namespace gem5
{

class KaguraController;

namespace compression
{
class ACC;
} // namespace compression

/**
 * A cache that feeds ACC's Global Compression Predictor.
 *
 * The predictor lives in the compressor. The compressor is called from
 * a non-virtual path in BaseCache, so it cannot observe hits itself; access()
 * is virtual, so a thin subclass can. This class classifies every hit to a
 * compressed block and updates the predictor:
 *
 *  - co-allocated (the block shares its superblock with another valid block):
 *    compression's extra capacity is holding this block, so the hit would
 *    otherwise have been a miss -> credit the miss penalty.
 *
 *  - alone in its superblock: the block would have been resident anyway and
 *    the access still pays decompression latency -> debit that latency, read
 *    from the block itself (the fill path records the real value there).
 *
 * Hits to uncompressed blocks say nothing about compression and leave the
 * predictor untouched.
 */
class ACCCache : public Cache
{
  protected:
    /** The predictor being fed */
    compression::ACC *acc;

    /**
     * Kagura's eviction feed. The controller tunes its
     * compression-disabling threshold by AIMD on eviction pressure, and
     * evictions are only visible here; every evicted block is reported and
     * the controller decides whether it counts.
     * The paper's architecture is D-cache-centric, so the
     * config sets this on the D-cache only.
     */
    KaguraController *kagura;

    /** Cycles credited per hit that compression made possible */
    const int64_t rewardCycles;

     /**
     * If reward_cycles is negative, measure the miss penalty at run time
     * instead of taking it from the config.
     *
     * A fixed number only fits the machine it was measured on. Change the
     * cache size and the miss penalty changes with it, so in a sweep every
     * row but the calibrated one gets the wrong reward and you can no
     * longer tell whether two rows differ because of the policy or because
     * of the calibration.
     */
    const bool autoReward;

    /** Miss penalty accumulated from responses, for the auto reward. */
    Tick missPenaltyTicks;
    /** Responses it was accumulated over. */
    uint64_t missPenaltySamples;

    struct ACCCacheStats : public statistics::Group
    {
        ACCCacheStats(statistics::Group *parent);

        /** Sampled miss penalty (sum), when the reward is measured. */
        statistics::Scalar missPenaltyTicks;
        /** Responses sampled. */
        statistics::Scalar missPenaltySamples;
        /** The measured miss penalty the GCP was actually rewarded with. */
        statistics::Formula avgMissPenaltyTicks;
    } accCacheStats;

    /** The GCP credit for one compression-enabled hit, in cycles. */
    int64_t currentReward() const;

    bool access(PacketPtr pkt, CacheBlk *&blk, Cycles &lat,
                PacketList &writebacks) override;

    [[nodiscard]] PacketPtr evictBlock(CacheBlk *blk) override;

    /** Samples the miss penalty on the way past */
    void recvTimingResp(PacketPtr pkt) override;

  public:
    typedef ACCCacheParams Params;
    ACCCache(const Params &p);
};

} // namespace gem5

#endif //__EHS_ACC_CACHE_HH__
