#!/usr/bin/env python3
"""
Stage 1 — Hardware Proof.

Minimal GNU Radio flowgraph that proves the SDRPlay RSP Duo talks to
gr-sdrplay3 and IQ samples are flowing:

    RSPduo source (single tuner) -> Qt GUI Frequency Sink + Waterfall Sink

Runs for 30 seconds on the 40 m band (~7.1 MHz) then exits. If the spectrum
and waterfall look sane on a known HF band, the environment is good and
development can proceed to Stage 2.

Requires: radioconda (GNU Radio 3.10.x) + gr-sdrplay3, with the SDRPlay API
service running. Launch from a radioconda prompt:

    python hardware_verify.py

NOTE: This is a first-pass flowgraph written before gr-sdrplay3 was installed
on the shack PC. Validate the sdrplay3.rspduo constructor/setter names against
the installed version the first time it runs, and adjust if the API differs.
"""

import signal
import sys

from PyQt5 import Qt
from gnuradio import gr, qtgui
from gnuradio.fft import window
import sip

try:
    from gnuradio import sdrplay3
except ImportError:
    sys.exit(
        "gr-sdrplay3 not found. Install it in radioconda:\n"
        "    conda install -c conda-forge gr-sdrplay3"
    )

# ---- Parameters -----------------------------------------------------------
SAMPLE_RATE = 2_000_000      # 2 MS/s (RSPduo single-tuner minimum output rate)
CENTER_FREQ = 7_100_000      # 40 m band, SSB portion
BANDWIDTH = 1_536_000        # IF bandwidth (Hz)
RUN_SECONDS = 30
FFT_SIZE = 2048


class HardwareVerify(gr.top_block, Qt.QWidget):
    def __init__(self):
        gr.top_block.__init__(self, "HF SDR — Stage 1 Hardware Verify")
        Qt.QWidget.__init__(self)
        self.setWindowTitle("HF SDR — Stage 1 Hardware Verify")
        self._layout = Qt.QVBoxLayout(self)

        # ---- RSP Duo source (single tuner) --------------------------------
        self.src = sdrplay3.rspduo(
            "",  # device selector (empty = first RSPduo found)
            rspduo_mode="Single Tuner",
            antenna="Tuner 1 50 ohm",
            stream_args=sdrplay3.stream_args(
                output_type="fc32",
                channels_size=1,
            ),
        )
        self.src.set_sample_rate(SAMPLE_RATE)
        self.src.set_center_freq(CENTER_FREQ)
        self.src.set_bandwidth(BANDWIDTH)
        # Gain values are gain REDUCTION in dB (higher = less gain):
        # IF [20-59], RF [0..]. Negative values are out of range and clamp to
        # minimum reduction (max gain) -> front-end overload on strong signals.
        self.src.set_gain_mode(False)        # AGC off for a predictable picture
        self.src.set_gain(40, "IF")          # IF gain reduction (dB), 40 = default
        self.src.set_gain(0, "RF")           # RF gain reduction (dB), raise if overloading
        self.src.set_dc_offset_mode(True)
        self.src.set_iq_balance_mode(True)
        self.src.set_rf_notch_filter(False)
        self.src.set_dab_notch_filter(False)

        # ---- Frequency (spectrum) sink ------------------------------------
        self.freq_sink = qtgui.freq_sink_c(
            FFT_SIZE, window.WIN_BLACKMAN_hARRIS,
            CENTER_FREQ, SAMPLE_RATE, "Spectrum", 1, None,
        )
        self.freq_sink.set_update_time(0.10)
        self.freq_sink.set_y_axis(-140, -20)
        self.freq_sink.enable_grid(True)
        self.freq_sink.enable_autoscale(False)
        self._add_widget(self.freq_sink)

        # ---- Waterfall sink -----------------------------------------------
        self.waterfall_sink = qtgui.waterfall_sink_c(
            FFT_SIZE, window.WIN_BLACKMAN_hARRIS,
            CENTER_FREQ, SAMPLE_RATE, "Waterfall", 1, None,
        )
        self.waterfall_sink.set_update_time(0.10)
        self.waterfall_sink.set_intensity_range(-140, -20)
        self._add_widget(self.waterfall_sink)

        # ---- Connections --------------------------------------------------
        self.connect((self.src, 0), (self.freq_sink, 0))
        self.connect((self.src, 0), (self.waterfall_sink, 0))

    def _add_widget(self, sink):
        win = sip.wrapinstance(sink.qwidget(), Qt.QWidget)
        self._layout.addWidget(win)


def main():
    qapp = Qt.QApplication(sys.argv)
    tb = HardwareVerify()
    tb.start()
    tb.show()

    def stop(*_):
        tb.stop()
        tb.wait()
        Qt.QApplication.quit()

    signal.signal(signal.SIGINT, stop)
    # Auto-exit after RUN_SECONDS so the proof is self-contained.
    Qt.QTimer.singleShot(RUN_SECONDS * 1000, stop)

    qapp.exec_()


if __name__ == "__main__":
    main()
