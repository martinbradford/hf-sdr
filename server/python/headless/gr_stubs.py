"""Inert GNU Radio stand-ins so server.py can be imported and unit-tested on a machine without
GNU Radio / gr-sdrplay3 (e.g. a laptop). Does nothing if the real packages are importable.

    from gr_stubs import install_gnuradio_stubs
    install_gnuradio_stubs()
    import server
"""
import sys
import types
from unittest import mock


def install_gnuradio_stubs():
    try:
        import gnuradio  # noqa: F401
        from gnuradio import sdrplay3  # noqa: F401
        return False
    except ImportError:
        pass

    class _Any(types.ModuleType):
        def __getattr__(self, name):
            return mock.MagicMock()

    def block_base(name):
        # permissive base class: `class X(gr.sync_block)` with gr.sync_block.__init__(self, ...) works
        return type(name, (), {"__init__": lambda self, *a, **k: None})

    gr = _Any("gnuradio")
    gr.gr = types.SimpleNamespace(top_block=block_base("top_block"),
                                  sync_block=block_base("sync_block"),
                                  basic_block=block_base("basic_block"),
                                  sizeof_gr_complex=8, sizeof_float=4)
    for sub in ("blocks", "analog", "fft", "filter", "sdrplay3"):
        setattr(gr, sub, _Any(f"gnuradio.{sub}"))
        sys.modules[f"gnuradio.{sub}"] = getattr(gr, sub)
    sys.modules["gnuradio"] = gr
    sys.modules["gnuradio.filter.firdes"] = mock.MagicMock()
    sys.modules["gnuradio.fft.window"] = mock.MagicMock()
    sys.modules["gnuradio.filter"].firdes = mock.MagicMock()
    sys.modules["gnuradio.fft"].window = mock.MagicMock()
    # the same base classes on the module itself, for `gnuradio.gr.X` style access
    for n in ("sync_block", "basic_block", "top_block", "hier_block2"):
        setattr(gr, n, block_base(n))
    return True
