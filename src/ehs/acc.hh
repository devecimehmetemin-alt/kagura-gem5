#ifndef __EHS_ACC_HH__
#define __EHS_ACC_HH__

#include <cstdint>
#include <memory>
#include <vector>

#include "base/statistics.hh"
#include "base/types.hh"
#include "ehs/energy_compressor.hh"
#include "params/ACC.hh"

namespace gem5
{
namespace compression
{

/**
 * Adaptive Cache Compression (Alameldeen & Wood, ISCA 2004).
 *
 * Compression helps when the capacity it adds turns misses into hits, and
 * hurts when blocks that would have been resident anyway pay decompression
 * latency (and, on this system, compression energy). ACC measures the balance
 * instead of guessing it: a Global Compression Predictor (GCP), one saturating
 * counter accumulating net cycles saved by compression. The cache credits it
 * with the miss penalty when a hit lands on a block that is only resident
 * because compression made room for it, and debits the decompression latency
 * when a hit lands on a compressed block that earned nothing. Fills compress
 * while the counter is positive (Compression Mode) and store uncompressed
 * while it is not (Regular Mode); a gated fill never runs the underlying
 * compressor, so Regular Mode also stops paying compression energy.
 */
class ACC : public EnergyCompressor
{
  protected:
    /** The GCP: net cycles compression has saved, saturating. */
    int64_t gcp;
    const int64_t gcpMin;
    const int64_t gcpMax;

    /** Gated fills between forced compressions in Regular Mode (0 = never). */
    const unsigned sampleInterval;
    unsigned gatedSinceSample;

    /**
     * GCP cycles debited when a compression attempt comes back full size
     * (0 = disabled).
     */
    const unsigned failedPenalty;

    struct ACCStats : public statistics::Group
    {
        ACCStats(statistics::Group *parent);

        /** GCP credits (hits attributed to compression). */
        statistics::Scalar rewards;
        /** GCP debits (decompression paid for nothing). */
        statistics::Scalar penalties;
        /** Fills stored uncompressed because the GCP said not to bother. */
        statistics::Scalar gatedCompressions;
        /** Regular Mode fills compressed anyway to keep evidence flowing. */
        statistics::Scalar sampledCompressions;
        /** Compression Mode <-> Regular Mode transitions. */
        statistics::Scalar modeSwitches;
        /** Fills stored uncompressed on Kagura's end-of-cycle override. */
        statistics::Scalar kaguraGatedCompressions;
        /** Attempts that came back full size and were charged to the GCP. */
        statistics::Scalar failedPenalties;
    } accStats;

    /**
     * Run the compressor and charge the GCP if the attempt failed. Every path
     * that compresses goes through here rather than calling runCompressor()
     * directly, so the sampling escape hatch is scored on the same terms as a
     * normal fill.
     */
    std::unique_ptr<Base::CompressionData> runAndScore(
        const std::vector<Chunk>& chunks, Cycles& comp_lat,
        Cycles& decomp_lat);

    std::unique_ptr<Base::CompressionData> compress(
        const std::vector<Chunk>& chunks, Cycles& comp_lat,
        Cycles& decomp_lat) override;

  public:
    typedef ACCParams Params;
    ACC(const Params &p);
    ~ACC() = default;

    /** Compression Mode while the GCP is positive. */
    bool compressionEnabled() const { return gcp > 0; }

    /** Credit the GCP: compression turned a miss into a hit. */
    void reward(int64_t cycles);

    /** Debit the GCP: decompression latency bought nothing. */
    void penalize(int64_t cycles);
};

} // namespace compression
} // namespace gem5

#endif //__EHS_ACC_HH__
