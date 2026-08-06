from m5.objects.MemCtrl import MemCtrl


class NvmMemCtrl(MemCtrl):
    """A MemCtrl that lets the power model read the traffic it has served.

    Main-memory traffic is the largest energy term on this machine

    MemCtrl already counts the bytes and only keeps them protected; this adds
    no parameters and no behaviour, just the two accessors.
    """

    type = "NvmMemCtrl"
    cxx_class = "gem5::memory::NvmMemCtrl"
    cxx_header = "ehs/nvm_mem_ctrl.hh"
