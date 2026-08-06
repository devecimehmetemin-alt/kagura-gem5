from m5.objects.Compressors import BaseCacheCompressor
from m5.params import *


class EnergyCompressor(BaseCacheCompressor):
    """Wraps a compressor and prices its work in energy."""

    type = "EnergyCompressor"
    cxx_class = "gem5::compression::EnergyCompressor"
    cxx_header = "ehs/energy_compressor.hh"

    compressor = Param.BaseCacheCompressor(
        "Compressor that does the actual work (e.g. BDI)"
    )

    e_compress = Param.Float("Energy of one compression, in J")
    e_decompress = Param.Float("Energy of one decompression, in J")

    dirty_aware = Param.Bool(
        False, "Exempt fills that serve a write from the Regular Mode gate"
    )

    # Software compressibility hint
    sw_hint_lo = Param.Addr(
        0, "Start of the address range software marks incompressible"
    )
    sw_hint_hi = Param.Addr(
        0, "End of that range, exclusive; lo == hi disables the hint"
    )
