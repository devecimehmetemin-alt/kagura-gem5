#ifndef __EHS_ENERGY_COMPRESSOR_HH__
#define __EHS_ENERGY_COMPRESSOR_HH__

#include <cstdint>
#include <memory>
#include <vector>

#include "base/statistics.hh"
#include "base/types.hh"
#include "mem/cache/compressors/base.hh"
#include "params/EnergyCompressor.hh"

namespace gem5
{

class BaseCache;

namespace compression
{

/**
 * A compressor that wraps another compressor and prices its work.
 *
 * gem5 models compression as pure latency: a compressor costs cycles, never
 * joules. We need to add power modelling to find what energy we need to
 * take off from the capacitor
 *
 * The wrapper is also where the compression-enabling decision is made
 */
class EnergyCompressor : public Base
{
  protected:
    /**
     * Compressed data envelope. The chunks are kept so the data can be
     * reconstructed without going back to the underlying compressor, whose
     * decompress() is protected.
     */
    class CompData : public Base::CompressionData
    {
      public:
        std::vector<Chunk> chunks;
    };

    /** The compressor that does the actual work. */
    Base *compressor;

    /**
     * Energy, in J, of one compression and one decompression from the paper
     */
    const double energyPerCompression;
    const double energyPerDecompression;

    /**
     * Kagura's mode override. When 1, every fill passes through
     * uncompressed. The controller has decided
     * the power cycle is about to end, Broadcast by the KaguraController.
     * It is distinct from ACC's own (GCP-driven) Regular Mode, which it
     * overrides.
     */
    bool kaguraRegular;

    /**
     * Exempt dirty fills from the Regular Mode gate. A dirty block is
     * written back to NVM by the checkpoint, so compressing it prepays at
     * the writeback. Without this, the uncompressed dirty set inflates the
     * checkpoint enough to turn Kagura into a net loss for caches of 2 kB
     * and up.
     */
    const bool dirtyAware;

    /**
     * True while the fill being compressed serves a write
     */
    bool fillServesWrite;

    /** Does the dirty exemption apply to the fill being compressed? */
    bool dirtyOverride() const { return dirtyAware && fillServesWrite; }

    /**
     * Software compressibility hint. A fill whose virtual address lands in
     * [swHintLo, swHintHi] is one software has marked incompressible
     * Equal bounds disable the hint.
     */
    const Addr swHintLo;
    const Addr swHintHi;

    /**
     * Virtual address of the fill being compressed, and whether the cache
     * knew one
     */
    Addr fillVaddr;
    bool fillVaddrValid;

    /** Does the hint mark the fill being compressed as incompressible? */
    bool hintSaysSkip() const
    {
        return swHintLo != swHintHi && fillVaddrValid &&
               fillVaddr >= swHintLo && fillVaddr < swHintHi;
    }

    /** Record what the hint saw on this fill */
    void noteHintAddr();

    /** stats */
    struct EnergyStats : public statistics::Group
    {
        EnergyStats(statistics::Group *parent);

        /** Compressions the underlying compressor actually ran. */
        statistics::Scalar compressionsRun;

        /** Fills the dirty exemption pulled past the Regular Mode gate. */
        statistics::Scalar dirtyOverrides;

        /** Fills the software hint said skip. */
        statistics::Scalar hintSkips;

        /** Fills the cache could give no virtual address for. */
        statistics::Scalar hintNoVaddr;

        /** Lowest and highest fill address the hint saw. */
        statistics::Scalar hintVaddrMin;
        statistics::Scalar hintVaddrMax;
    } energyStats;

    /**
     * Run the underlying compressor on the line and account the energy.
     * This is the path a fill takes when compression is on.
     */
    std::unique_ptr<Base::CompressionData> runCompressor(
        const std::vector<Chunk>& chunks, Cycles& comp_lat,
        Cycles& decomp_lat);

    /** Decline to compress with full-size result, no latency, no energy. */
    std::unique_ptr<Base::CompressionData> passThrough(
        const std::vector<Chunk>& chunks, Cycles& comp_lat,
        Cycles& decomp_lat);

    std::unique_ptr<Base::CompressionData> compress(
        const std::vector<Chunk>& chunks, Cycles& comp_lat,
        Cycles& decomp_lat) override;

    void decompress(const CompressionData* comp_data,
                    uint64_t* cache_line) override;

  public:
    typedef EnergyCompressorParams Params;
    EnergyCompressor(const Params &p);
    ~EnergyCompressor() = default;

    void setCache(BaseCache *_cache) override;

    /** Energy, in J, spent on compression and decompression so far. */
    double getEnergy() const;

    /** Kagura's mode broadcast: true forces uncompressed fills */
    void setRegularMode(bool regular) { kaguraRegular = regular; }

    /** Set around a write-serving fill; the cache must clear it after. */
    void setFillServesWrite(bool serves) { fillServesWrite = serves; }

    /** The cache's address signal for the software hint */
    void setFillVaddr(Addr vaddr, bool valid)
    { fillVaddr = vaddr; fillVaddrValid = valid; }
};

} // namespace compression
} // namespace gem5

#endif //__EHS_ENERGY_COMPRESSOR_HH__
