#!/usr/bin/env python3
"""
Stage 1 — Hardware Proof + live monitor.

    RSPduo source ─┬─→ Qt Frequency Sink   (spectrum)
                   ├─→ Qt Waterfall Sink   (waterfall)
                   └─→ tune → sideband filter → complex_to_real → AGC
                        → resample → audio out   (listen to band centre)

Proves the RSP Duo talks to gr-sdrplay3 AND lets you fine-tune by ear: the
frequency readout shows the current tuned frequency (= display centre = what
you hear), and the -/+ buttons retune in 500 Hz steps. Bring a signal to the
centre line of the waterfall, then nudge to zero-beat it.

Requires: radioconda (GNU Radio 3.10.x) + gr-sdrplay3, SDRPlay API running.
Launch from a radioconda prompt:

    python hardware_verify.py                    # start at 7.1 MHz, LSB
    python hardware_verify.py --freq 7.2e6 --mode usb

Notes:
- Tuning is 500 Hz/step (fine); pass --freq to jump near a signal first.
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
STEP_HZ = 500                # fine tuning step for the -/+ buttons
FFT_SIZE = 2048


class HardwareMonitor(gr.top_block, Qt.QWidget):
    def __init__(self, freq, mode, agc, if_gr, rf_gr):
        gr.top_block.__init__(self, "HF SDR — Stage 1 Monitor")
        Qt.QWidget.__init__(self)
        self.setWindowTitle("HF SDR — Stage 1 Monitor")
        self._layout = Qt.QVBoxLayout(self)

        self.vfo = freq            # tuned freq = hardware centre = display centre
        self.mode = mode

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

        # ---- Spectrum + waterfall (on the raw wideband stream) ------------
        self.freq_sink = qtgui.freq_sink_c(
            FFT_SIZE, window.WIN_BLACKMAN_hARRIS,
            self.vfo, SOURCE_RATE, "Spectrum", 1, None)
        self.freq_sink.set_update_time(0.10)
        self.freq_sink.set_y_axis(-140, -20)
        self.freq_sink.enable_grid(True)
        self.freq_sink.enable_autoscale(False)
        self._add_widget(self.freq_sink)

        self.waterfall = qtgui.waterfall_sink_c(
            FFT_SIZE, window.WIN_BLACKMAN_hARRIS,
            self.vfo, SOURCE_RATE, "Waterfall", 1, None)
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

        # ---- Tuning controls ---------------------------------------------
        controls = Qt.QHBoxLayout()
        down_btn = Qt.QPushButton("◀  −500 Hz")
        up_btn = Qt.QPushButton("+500 Hz  ▶")
        self.freq_label = Qt.QLabel()
        f = self.freq_label.font(); f.setPointSize(16); f.setBold(True)
        self.freq_label.setFont(f)
        self.freq_label.setAlignment(Qt.Qt.AlignCenter)
        down_btn.clicked.connect(self._tune_down)
        up_btn.clicked.connect(self._tune_up)
        controls.addWidget(down_btn)
        controls.addWidget(self.freq_label, 1)
        controls.addWidget(up_btn)
        self._layout.addLayout(controls)
        self._update_label()

        # ---- Connections --------------------------------------------------
        self.connect(self.src, self.freq_sink)
        self.connect(self.src, self.waterfall)
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
        self.freq_sink.set_frequency_range(self.vfo, SOURCE_RATE)
        self.waterfall.set_frequency_range(self.vfo, SOURCE_RATE)
        self._update_label()

    def _tune_up(self):
        self._retune(self.vfo + STEP_HZ)

    def _tune_down(self):
        self._retune(self.vfo - STEP_HZ)

    def _update_label(self):
        self.freq_label.setText(f"{self.vfo/1e3:,.1f} kHz   {self.mode.upper()}")


def main():
    p = argparse.ArgumentParser(description="Stage 1 hardware monitor")
    p.add_argument("--freq", type=float, default=7.1e6,
                   help="initial tuned frequency, Hz (default 7.1e6)")
    p.add_argument("--mode", choices=["lsb", "usb"], default="lsb",
                   help="sideband (default lsb, correct for 40 m)")
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
    tb = HardwareMonitor(args.freq, args.mode, args.agc, args.if_gr, args.rf_gr)
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
