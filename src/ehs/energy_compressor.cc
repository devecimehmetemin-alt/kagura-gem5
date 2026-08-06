#include "ehs/energy_compressor.hh"

#include <vector>

#include "params/EnergyCompressor.hh"

namespace gem5
{
namespace compression
{

EnergyCompressor::EnergyCompressor(const Params &p)
  : Base(p),
    compressor(p.compressor),
    energyPerCompression(p.e_compress),
    energyPerDecompression(p.e_decompress),
    kaguraRegular(false),
    dirtyAware(p.dirty_aware),
    fillServesWrite(false),
    swHintLo(p.sw_hint_lo),
    swHintHi(p.sw_hint_hi),
    fillVaddr(0),
    fillVaddrValid(false),
    energyStats(this)
{
}

void
EnergyCompressor::setCache(BaseCache *_cache)
{
    Base::setCache(_cache);

    // The wrapped compressor is connected to the same cache
    compressor->setCache(_cache);
}

std::unique_ptr<Base::CompressionData>
EnergyCompressor::runCompressor(const std::vector<Chunk>& chunks,
                                Cycles& comp_lat, Cycles& decomp_lat)
{
    auto comp_data = std::make_unique<CompData>();
    comp_data->chunks = chunks;

    // Rebuild the line and hand it to the underlying compressor through
    // its public entry point.
    std::vector<uint64_t> data(blkSize / sizeof(uint64_t));
    fromChunks(chunks, data.data());

    const auto sub_data = compressor->compress(data.data(), comp_lat,
                                               decomp_lat);
    energyStats.compressionsRun++;

    comp_data->setSizeBits(sub_data->getSizeBits());

    return comp_data;
}

void
EnergyCompressor::noteHintAddr()
{
    if (swHintLo == swHintHi)
        return;

    if (!fillVaddrValid) {
        energyStats.hintNoVaddr++;
        return;
    }

    if (energyStats.hintVaddrMin.value() == 0 ||
        fillVaddr < energyStats.hintVaddrMin.value())
        energyStats.hintVaddrMin = fillVaddr;
    if (fillVaddr > energyStats.hintVaddrMax.value())
        energyStats.hintVaddrMax = fillVaddr;
}

std::unique_ptr<Base::CompressionData>
EnergyCompressor::passThrough(const std::vector<Chunk>& chunks,
                              Cycles& comp_lat, Cycles& decomp_lat)
{
    auto comp_data = std::make_unique<CompData>();
    comp_data->chunks = chunks;

    // A full-size result is what a failed compression produces, so the block
    // is stored uncompressed, pays no decompression latency, and can never
    // co-allocate
    comp_data->setSizeBits(blkSize * 8);
    comp_lat = Cycles(0);
    decomp_lat = Cycles(0);

    return comp_data;
}

std::unique_ptr<Base::CompressionData>
EnergyCompressor::compress(const std::vector<Chunk>& chunks, Cycles& comp_lat,
                           Cycles& decomp_lat)
{
    noteHintAddr();

    if (hintSaysSkip()) {
        energyStats.hintSkips++;
        return passThrough(chunks, comp_lat, decomp_lat);
    }

    // Kagura mode
    if (kaguraRegular) {
        if (!dirtyOverride())
            return passThrough(chunks, comp_lat, decomp_lat);
        energyStats.dirtyOverrides++;
    }

    return runCompressor(chunks, comp_lat, decomp_lat);
}

void
EnergyCompressor::decompress(const CompressionData* comp_data,
                             uint64_t* cache_line)
{
    const auto *data = static_cast<const CompData *>(comp_data);
    fromChunks(data->chunks, cache_line);
}

double
EnergyCompressor::getEnergy() const
{
    // Calculate energy consumption
    return energyStats.compressionsRun.value() * energyPerCompression +
           stats.decompressions.value() * energyPerDecompression;
}

EnergyCompressor::EnergyStats::EnergyStats(statistics::Group *parent)
  : statistics::Group(parent),
    ADD_STAT(compressionsRun, statistics::units::Count::get(),
             "Compressions the underlying compressor actually ran "
             "(the inherited compressions stat counts attempts)"),
    ADD_STAT(dirtyOverrides, statistics::units::Count::get(),
             "Fills the dirty exemption pulled past the Regular Mode gate"),
    ADD_STAT(hintSkips, statistics::units::Count::get(),
             "Fills the software compressibility hint kept away from the "
             "compressor"),
    ADD_STAT(hintNoVaddr, statistics::units::Count::get(),
             "Fills the cache could give no virtual address for"),
    ADD_STAT(hintVaddrMin, statistics::units::Count::get(),
             "Lowest fill address the hint saw"),
    ADD_STAT(hintVaddrMax, statistics::units::Count::get(),
             "Highest fill address the hint saw")
{
}

} // namespace compression
} // namespace gem5
