#include "ehs/acc.hh"

#include <algorithm>

#include "base/logging.hh"
#include "base/trace.hh"
#include "debug/ACC.hh"
#include "params/ACC.hh"

namespace gem5
{
namespace compression
{

ACC::ACC(const Params &p)
  : EnergyCompressor(p),
    gcp(p.gcp_init),
    gcpMin(p.gcp_min),
    gcpMax(p.gcp_max),
    sampleInterval(p.sample_interval),
    gatedSinceSample(0),
    failedPenalty(p.failed_penalty),
    accStats(this)
{
    fatal_if(gcpMin >= gcpMax, "GCP saturation floor must be below ceiling");
    fatal_if(gcp < gcpMin || gcp > gcpMax,
             "Initial GCP outside its saturation range");
}

std::unique_ptr<Base::CompressionData>
ACC::runAndScore(const std::vector<Chunk>& chunks, Cycles& comp_lat,
                 Cycles& decomp_lat)
{
    auto comp_data = runCompressor(chunks, comp_lat, decomp_lat);

    // A full size result never coallocates and is never marked compressed,
    // so it generates neither a reward nor a penalty for the rest of its
    // life. The GCP only ever sees attempts that succeeded. The original
    // method does not need a penalty here because it runs the compressor
    // on every allocation whether or not it stores the result. Compression
    // cost is a constant there, and the computed size still feeds its
    // avoidable-miss test, so incompressible data simply stops earning
    // rewards.

    if (failedPenalty && comp_data->getSizeBits() >= blkSize * 8) {
        accStats.failedPenalties++;
        penalize(failedPenalty);
    }

    return comp_data;
}

std::unique_ptr<Base::CompressionData>
ACC::compress(const std::vector<Chunk>& chunks, Cycles& comp_lat,
              Cycles& decomp_lat)
{
    // The software compressibility hint outranks every policy below it
    noteHintAddr();
    if (hintSaysSkip()) {
        energyStats.hintSkips++;
        return passThrough(chunks, comp_lat, decomp_lat);
    }

    // Kagura's end-of-cycle override outranks everything below,
    if (kaguraRegular) {
        if (!dirtyOverride()) {
            accStats.kaguraGatedCompressions++;
            return passThrough(chunks, comp_lat, decomp_lat);
        }
        energyStats.dirtyOverrides++;
    }

    if (compressionEnabled()) {
        return runAndScore(chunks, comp_lat, decomp_lat);
    }

    // Regular Mode. Compress once every sample_interval fill anyway: a gated
    // fill can never co-allocate, so without sampling no reward could ever
    // arrive and the predictor would wedge at its floor.
    if (sampleInterval && ++gatedSinceSample >= sampleInterval) {
        gatedSinceSample = 0;
        accStats.sampledCompressions++;
        return runAndScore(chunks, comp_lat, decomp_lat);
    }

    accStats.gatedCompressions++;
    return passThrough(chunks, comp_lat, decomp_lat);
}

void
ACC::reward(int64_t cycles)
{
    const bool was_enabled = compressionEnabled();

    gcp = std::min(gcp + cycles, gcpMax);
    accStats.rewards++;

    if (compressionEnabled() != was_enabled) {
        accStats.modeSwitches++;
        DPRINTF(ACC, "GCP=%lld: entering Compression Mode\n", gcp);
    }
}

void
ACC::penalize(int64_t cycles)
{
    const bool was_enabled = compressionEnabled();

    gcp = std::max(gcp - cycles, gcpMin);
    accStats.penalties++;

    if (compressionEnabled() != was_enabled) {
        accStats.modeSwitches++;
        DPRINTF(ACC, "GCP=%lld: entering Regular Mode\n", gcp);
    }
}

ACC::ACCStats::ACCStats(statistics::Group *parent)
  : statistics::Group(parent),
    ADD_STAT(rewards, statistics::units::Count::get(),
             "GCP credits: hits attributed to compression's extra capacity"),
    ADD_STAT(penalties, statistics::units::Count::get(),
             "GCP debits: decompression latency paid for nothing"),
    ADD_STAT(gatedCompressions, statistics::units::Count::get(),
             "Fills stored uncompressed because the GCP said not to bother"),
    ADD_STAT(sampledCompressions, statistics::units::Count::get(),
             "Regular Mode fills compressed anyway to keep evidence flowing"),
    ADD_STAT(modeSwitches, statistics::units::Count::get(),
             "Compression Mode <-> Regular Mode transitions"),
    ADD_STAT(kaguraGatedCompressions, statistics::units::Count::get(),
             "Fills stored uncompressed on Kagura's end-of-cycle override"),
    ADD_STAT(failedPenalties, statistics::units::Count::get(),
             "Compression attempts that came back full size and were charged "
             "to the GCP (0 unless failed_penalty is set)")
{
}

} // namespace compression
} // namespace gem5
