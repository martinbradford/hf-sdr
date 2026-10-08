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

    gr = _Any("gnuradio")
    gr.gr = types.SimpleNamespace(top_block=type("top_block", (), {}),
                                  sync_block=type("sync_block", (), {}),
                                  basic_block=type("basic_block", (), {}),
                                  sizeof_gr_complex=8, sizeof_float=4)
    for sub in ("blocks", "analog", "fft", "filter", "sdrplay3"):
        setattr(gr, sub, _Any(f"gnuradio.{sub}"))
        sys.modules[f"gnuradio.{sub}"] = getattr(gr, sub)
    sys.modules["gnuradio"] = gr
    sys.modules["gnuradio.filter.firdes"] = mock.MagicMock()
    sys.modules["gnuradio.fft.window"] = mock.MagicMock()
    sys.modules["gnuradio.filter"].firdes = mock.MagicMock()
    sys.modules["gnuradio.fft"].window = mock.MagicMock()
    # block base classes used with `class X(gr.sync_block)` etc.
    for n in ("sync_block", "basic_block", "top_block", "hier_block2"):
        setattr(gr, n, type(n, (), {"__init__": lambda self, *a, **k: None}))
    return True
