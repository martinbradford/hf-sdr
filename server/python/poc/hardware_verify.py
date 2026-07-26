#!/usr/bin/env python3
"""
Stage 1 — Hardware Proof + live monitor.

    RSPduo source ─┬─→ Qt Frequency Sink   (spectrum)
                   ├─→ Qt Waterfall Sink   (waterfall)
                   └─→ tune → sideband filter → complex_to_real → AGC
                        → resample → audio out   (listen to band centre)

Proves the RSP Duo talks to gr-sdrplay3 AND lets you tune by ear. The
frequency readout shows the current tuned frequency (= display centre = what
you hear). Tuning buttons step in 100 kHz / 5 kHz / 500 Hz (down and up), and
LSB/USB buttons switch sideband live. Roam with the coarse buttons, bring a
signal to the centre line of the waterfall, then nudge with 500 Hz to
zero-beat it.

Requires: radioconda (GNU Radio 3.10.x) + gr-sdrplay3, SDRPlay API running.
Launch from a radioconda prompt:

    python hardware_verify.py                    # start at 7.1 MHz, LSB
    python hardware_verify.py --freq 7.2e6 --mode usb

Notes:
- The display shows a decimated (zoomed) copy of the stream; set the width
  with --span-khz (default 250). The audio still uses the full-rate source.
- The sideband filter (200-2700 Hz) rejects the DC/LO spike, so listening at
  centre is clean.
"""

import argparse
import signal
import sys

from PyQt5 import Qt
from gnuradio import gr, qtgui, blocks, analog, audio
from gnuradio import filter as gr_filter
from gnuradio.filter import firdes
from gnuradio.fft import window
import sip

try:
    from gnuradio import sdrplay3
except ImportError:
    sys.exit(
        "gr-sdrplay3 not found. Install it per docs/SETUP_NOTES.md:\n"
        "    conda install <gnuradio-sdrplay3 .conda from fventuri releases>"
    )

# ---- Parameters -----------------------------------------------------------
SOURCE_RATE = 2_000_000      # RSPduo output rate
DECIM = 40                   # -> 50 kHz intermediate rate
INTER_RATE = SOURCE_RATE // DECIM
AUDIO_RATE = 48_000
FINE_STEP = 500              # fine (zero-beat) tuning step, Hz
MED_STEP = 5_000             # medium tuning step, Hz
COARSE_STEP = 100_000        # coarse (roam) tuning step, Hz
FFT_SIZE = 2048


class HardwareMonitor(gr.top_block, Qt.QWidget):
    def __init__(self, freq, mode, agc, if_gr, rf_gr, span_hz):
        gr.top_block.__init__(self, "HF SDR — Stage 1 Monitor")
        Qt.QWidget.__init__(self)
        self.setWindowTitle("HF SDR — Stage 1 Monitor")
        self._layout = Qt.QVBoxLayout(self)

        self.vfo = freq            # tuned freq = hardware centre = display centre
        self.mode = mode

        # Display span: decimate a copy of the stream so the sinks show a
        # narrower window (default ~250 kHz) instead of the full 2 MHz.
        self.disp_decim = max(1, round(SOURCE_RATE / span_hz))
        self.display_rate = SOURCE_RATE / self.disp_decim

        # ---- RSP Duo source (single tuner) --------------------------------
        self.src = sdrplay3.rspduo(
            "", rspduo_mode="Single Tuner", antenna="Tuner 1 50 ohm",
            stream_args=sdrplay3.stream_args(output_type="fc32", channels_size=1),
        )
        self.src.set_sample_rate(SOURCE_RATE)
        self.src.set_center_freq(self.vfo)
        self.src.set_bandwidth(1_536_000)
        # Gain is gain REDUCTION in dB (higher = less gain): IF [20-59], RF [0..].
        # IF AGC auto-manages IF reduction to avoid ADC overload.
        self.src.set_gain_mode(agc)
        if agc:
            self.src.set_agc_setpoint(-30)
        else:
            self.src.set_gain(if_gr, "IF")
        self.src.set_gain(rf_gr, "RF")
        self.src.set_dc_offset_mode(True)
        self.src.set_iq_balance_mode(True)

        # ---- Display decimator (zoom) ------------------------------------
        disp_taps = firdes.low_pass(
            1.0, SOURCE_RATE, self.display_rate * 0.45, self.display_rate * 0.10)
        self.disp_filter = gr_filter.fir_filter_ccf(self.disp_decim, disp_taps)

        # ---- Spectrum + waterfall (on the decimated/zoomed stream) --------
        self.freq_sink = qtgui.freq_sink_c(
            FFT_SIZE, window.WIN_BLACKMAN_hARRIS,
            self.vfo, self.display_rate, "Spectrum", 1, None)
        self.freq_sink.set_update_time(0.10)
        self.freq_sink.set_y_axis(-140, -20)
        self.freq_sink.enable_grid(True)
        self.freq_sink.enable_autoscale(False)
        self._add_widget(self.freq_sink)

        self.waterfall = qtgui.waterfall_sink_c(
            FFT_SIZE, window.WIN_BLACKMAN_hARRIS,
            self.vfo, self.display_rate, "Waterfall", 1, None)
        self.waterfall.set_update_time(0.10)
        self.waterfall.set_intensity_range(-140, -20)
        self._add_widget(self.waterfall)

        # ---- Demod chain: tune (offset 0) -> sideband -> audio ------------
        xlate_taps = firdes.low_pass(1.0, SOURCE_RATE, 15_000, 5_000)
        self.xlate = gr_filter.freq_xlating_fir_filter_ccf(
            DECIM, xlate_taps, 0.0, SOURCE_RATE)   # demod at centre
        self.sideband = gr_filter.fir_filter_ccc(1, self._sideband_taps())
        self.to_real = blocks.complex_to_real(1)
        self.agc = analog.agc2_ff(1e-1, 1e-2, 0.3, 1.0)
        self.agc.set_max_gain(65_536)
        self.resamp = gr_filter.rational_resampler_fff(
            interpolation=AUDIO_RATE // 1_000, decimation=INTER_RATE // 1_000)
        self.vol = blocks.multiply_const_ff(0.5)
        self.audio_sink = audio.sink(AUDIO_RATE, "", True)

        # ---- Frequency readout -------------------------------------------
        self.freq_label = Qt.QLabel()
        f = self.freq_label.font(); f.setPointSize(16); f.setBold(True)
        self.freq_label.setFont(f)
        self.freq_label.setAlignment(Qt.Qt.AlignCenter)

        # ---- Tuning bar: coarse / medium / fine, down then up ------------
        tuning = Qt.QHBoxLayout()
        down_steps = (("◀ −100k", -COARSE_STEP), ("◀ −5k", -MED_STEP),
                      ("◀ −500", -FINE_STEP))
        up_steps = (("+500 ▶", FINE_STEP), ("+5k ▶", MED_STEP),
                    ("+100k ▶", COARSE_STEP))
        for text, delta in down_steps:
            b = Qt.QPushButton(text)
            b.clicked.connect(lambda _, d=delta: self._retune(self.vfo + d))
            tuning.addWidget(b)
        tuning.addWidget(self.freq_label, 1)
        for text, delta in up_steps:
            b = Qt.QPushButton(text)
            b.clicked.connect(lambda _, d=delta: self._retune(self.vfo + d))
            tuning.addWidget(b)
        self._layout.addLayout(tuning)

        # ---- Mode: LSB / USB (exclusive) ---------------------------------
        modes = Qt.QHBoxLayout()
        modes.addStretch(1)
        self.mode_group = Qt.QButtonGroup(self)
        self.mode_group.setExclusive(True)
        for m in ("lsb", "usb"):
            b = Qt.QPushButton(m.upper())
            b.setCheckable(True)
            b.setChecked(m == self.mode)
            b.clicked.connect(lambda _, mm=m: self._set_mode(mm))
            self.mode_group.addButton(b)
            modes.addWidget(b)
        modes.addStretch(1)
        self._layout.addLayout(modes)

        self._update_label()

        # ---- Connections --------------------------------------------------
        # Display: decimate a copy for the zoomed spectrum/waterfall.
        self.connect(self.src, self.disp_filter)
        self.connect(self.disp_filter, self.freq_sink)
        self.connect(self.disp_filter, self.waterfall)
        # Audio: full-rate source -> demod chain.
        self.connect(self.src, self.xlate, self.sideband, self.to_real,
                     self.agc, self.resamp, self.vol, self.audio_sink)

    def _sideband_taps(self):
        low, high = (-2_700, -200) if self.mode == "lsb" else (200, 2_700)
        return firdes.complex_band_pass(1.0, INTER_RATE, low, high, 200)

    def _add_widget(self, sink):
        self._layout.addWidget(sip.wrapinstance(sink.qwidget(), Qt.QWidget))

    def _retune(self, hz):
        self.vfo = hz
        self.src.set_center_freq(self.vfo)
        self.freq_sink.set_frequency_range(self.vfo, self.display_rate)
        self.waterfall.set_frequency_range(self.vfo, self.display_rate)
        self._update_label()

    def _set_mode(self, mode):
        self.mode = mode
        self.sideband.set_taps(self._sideband_taps())   # live sideband swap
        self._update_label()

    def _update_label(self):
        self.freq_label.setText(f"{self.vfo/1e3:,.1f} kHz   {self.mode.upper()}")


def main():
    p = argparse.ArgumentParser(description="Stage 1 hardware monitor")
    p.add_argument("--freq", type=float, default=7.1e6,
                   help="initial tuned frequency, Hz (default 7.1e6)")
    p.add_argument("--mode", choices=["lsb", "usb"], default="lsb",
                   help="sideband (default lsb, correct for 40 m)")
    p.add_argument("--span-khz", type=float, default=250,
                   help="display width in kHz (default 250; smaller = more zoom)")
    p.add_argument("--rf-gr", type=int, default=0,
                   help="RF gain reduction dB (raise to fix overload)")
    p.add_argument("--if-gr", type=int, default=40,
                   help="IF gain reduction dB [20-59], used only with --no-agc")
    agc_grp = p.add_mutually_exclusive_group()
    agc_grp.add_argument("--agc", dest="agc", action="store_true", default=True,
                         help="enable IF AGC (default)")
    agc_grp.add_argument("--no-agc", dest="agc", action="store_false",
                         help="disable IF AGC and use --if-gr")
    args = p.parse_args()

    qapp = Qt.QApplication(sys.argv)
    tb = HardwareMonitor(args.freq, args.mode, args.agc, args.if_gr, args.rf_gr,
                         args.span_khz * 1_000)
    tb.start()
    tb.show()

    def stop(*_):
        tb.stop(); tb.wait(); Qt.QApplication.quit()
    signal.signal(signal.SIGINT, stop)
    # Periodic no-op timer so Python can service Ctrl-C during the Qt loop.
    timer = Qt.QTimer(); timer.start(200); timer.timeout.connect(lambda: None)

    qapp.exec_()


if __name__ == "__main__":
    main()
