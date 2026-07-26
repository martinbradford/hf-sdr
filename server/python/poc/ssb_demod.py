#!/usr/bin/env python3
"""
Stage 2 — Demodulation Proof (SSB).

    RSPduo source -> freq-xlating tuner/decimator -> sideband filter
                  -> complex_to_real -> AGC -> resample -> audio out

Tune to a 40 m SSB signal (LSB by default) and verify the audio sounds correct.
Find a busy frequency on the Stage 1 waterfall, then pass it in:

    python ssb_demod.py --freq 7.150e6            # 40 m is LSB
    python ssb_demod.py --freq 14.250e6 --mode usb --center 14.2e6

Runs until Ctrl-C.

NOTE: PoC only. All the "real" DSP will move server-side (Stage 4+); this just
proves the demod chain and audio path on this machine.
"""

import argparse
import signal
import sys

from gnuradio import gr, blocks, analog, audio
from gnuradio import filter as gr_filter
from gnuradio.filter import firdes

try:
    from gnuradio import sdrplay3
except ImportError:
    sys.exit("gr-sdrplay3 not found. See docs/SETUP_NOTES.md.")

SOURCE_RATE = 2_000_000      # RSPduo output rate
DECIM = 40                   # -> 50 kHz intermediate rate
INTER_RATE = SOURCE_RATE // DECIM
AUDIO_RATE = 48_000


class SsbReceiver(gr.top_block):
    def __init__(self, center, freq, mode, volume):
        gr.top_block.__init__(self, "HF SDR — Stage 2 SSB Demod")

        offset = freq - center  # freq-xlating filter brings this to baseband

        # ---- Source ------------------------------------------------------
        self.src = sdrplay3.rspduo(
            "", rspduo_mode="Single Tuner", antenna="Tuner 1 50 ohm",
            stream_args=sdrplay3.stream_args(output_type="fc32", channels_size=1),
        )
        self.src.set_sample_rate(SOURCE_RATE)
        self.src.set_center_freq(center)
        self.src.set_bandwidth(1_536_000)
        self.src.set_gain_mode(False)
        self.src.set_gain(-40, "IF")
        self.src.set_gain(-20, "RF")
        self.src.set_dc_offset_mode(True)
        self.src.set_iq_balance_mode(True)

        # ---- Tune + decimate to 50 kHz -----------------------------------
        xlate_taps = firdes.low_pass(1.0, SOURCE_RATE, 15_000, 5_000)
        self.xlate = gr_filter.freq_xlating_fir_filter_ccf(
            DECIM, xlate_taps, offset, SOURCE_RATE)

        # ---- Select one sideband (complex band-pass) ---------------------
        if mode == "lsb":
            low_cut, high_cut = -2_700, -200
        else:  # usb
            low_cut, high_cut = 200, 2_700
        sb_taps = firdes.complex_band_pass(
            1.0, INTER_RATE, low_cut, high_cut, 200)
        self.sideband = gr_filter.fir_filter_ccc(1, sb_taps)

        # ---- To real audio, AGC, resample to 48 kHz, volume --------------
        self.to_real = blocks.complex_to_real(1)
        self.agc = analog.agc2_ff(1e-1, 1e-2, 0.3, 1.0)
        self.agc.set_max_gain(65_536)
        self.resamp = gr_filter.rational_resampler_fff(
            interpolation=AUDIO_RATE // 1_000, decimation=INTER_RATE // 1_000)
        self.vol = blocks.multiply_const_ff(volume)
        self.audio_sink = audio.sink(AUDIO_RATE, "", True)

        # ---- Wire it up --------------------------------------------------
        self.connect(self.src, self.xlate, self.sideband, self.to_real,
                     self.agc, self.resamp, self.vol, self.audio_sink)


def main():
    p = argparse.ArgumentParser(description="Stage 2 SSB demod PoC")
    p.add_argument("--freq", type=float, required=True,
                   help="signal frequency to tune, Hz (e.g. 7.150e6)")
    p.add_argument("--center", type=float, default=7.1e6,
                   help="RSPduo center frequency, Hz (default 7.1e6)")
    p.add_argument("--mode", choices=["lsb", "usb"], default="lsb",
                   help="sideband (default lsb, correct for 40 m)")
    p.add_argument("--volume", type=float, default=0.5, help="output volume")
    args = p.parse_args()

    if abs(args.freq - args.center) > SOURCE_RATE / 2:
        sys.exit(f"--freq must be within +/- {SOURCE_RATE/2e6:.1f} MHz of --center")

    tb = SsbReceiver(args.center, args.freq, args.mode, args.volume)
    print(f"Tuned {args.freq/1e6:.4f} MHz ({args.mode.upper()}), "
          f"center {args.center/1e6:.3f} MHz. Ctrl-C to stop.")
    tb.start()

    def stop(*_):
        tb.stop(); tb.wait(); sys.exit(0)
    signal.signal(signal.SIGINT, stop)
    tb.wait()


if __name__ == "__main__":
    main()
