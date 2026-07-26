#!/usr/bin/env python3
"""
Multiple virtual receivers (VRX) within one band — single-tuner mode.

One RSPduo single-tuner capture (~2 MHz) fans out to several independent demod
chains, each freq-xlating-tuned to a different signal inside the captured
window:

    RSPduo single tuner ─┬─ VRX0: xlate(f0-center) → SSB → audio
                         ├─ VRX1: xlate(f1-center) → SSB → audio
                         └─ ...

This is the right way to run several receivers in the SAME band — no dual-tuner
mode needed, so none of the dual-tuner init fragility. (Independent dual-tuner
mode is only for receivers on DIFFERENT bands.) It also mirrors the planned
lock()/unlock() "add/remove receiver chains" architecture.

Audio routing: 1 VRX → mono; 2 VRX → stereo (VRX0=left, VRX1=right);
3+ VRX → summed to mono. All VRXs must fall within +/- ~0.9 MHz of --center.

    python multi_vrx.py --center 7.15e6 --vrx 7.150e6 --vrx 7.175e6
    python multi_vrx.py --center 14.1e6 --vrx 14.074e6 --vrx 14.095e6 --ssb usb
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

SOURCE_RATE = 2_000_000
DECIM = 40
INTER_RATE = SOURCE_RATE // DECIM
AUDIO_RATE = 48_000
MAX_OFFSET = SOURCE_RATE / 2 - 50_000     # usable half-window (Hz)


def add_vrx(tb, block, port, offset, ssb, bw):
    """One VRX: freq-xlate to `offset`, SSB filter, to audio-rate float."""
    xlate = gr_filter.freq_xlating_fir_filter_ccf(
        DECIM, firdes.low_pass(1.0, SOURCE_RATE, 15_000, 5_000), offset, SOURCE_RATE)
    lo_edge = 300
    low, high = (-bw, -lo_edge) if ssb == "lsb" else (lo_edge, bw)
    sb = gr_filter.fir_filter_ccc(1, firdes.complex_band_pass(1.0, INTER_RATE, low, high, 200))
    c2r = blocks.complex_to_real(1)
    ag = analog.agc2_ff(1e-1, 1e-2, 0.3, 1.0); ag.set_max_gain(1_024)
    rs = gr_filter.rational_resampler_fff(interpolation=AUDIO_RATE // 1_000,
                                          decimation=INTER_RATE // 1_000)
    vol = blocks.multiply_const_ff(0.5)
    tb.connect((block, port), xlate, sb, c2r, ag, rs, vol)
    return vol


class MultiVRX(gr.top_block):
    def __init__(self, center, vrx_freqs, ssb, bw, agc, if_gr, rf_gr):
        gr.top_block.__init__(self, "HF SDR — multi-VRX (single tuner)")

        self.src = sdrplay3.rspduo(
            "", rspduo_mode="Single Tuner", antenna="Tuner 1 50 ohm",
            stream_args=sdrplay3.stream_args(output_type="fc32", channels_size=1))
        self.src.set_sample_rate(SOURCE_RATE)
        self.src.set_center_freq(center)
        self.src.set_bandwidth(1_536_000)
        self.src.set_gain_mode(agc)
        if agc:
            self.src.set_agc_setpoint(-30)
        else:
            self.src.set_gain(if_gr, "IF")
        self.src.set_gain(rf_gr, "RF")
        self.src.set_dc_offset_mode(True)
        self.src.set_iq_balance_mode(True)

        outs = [add_vrx(self, self.src, 0, f - center, ssb, bw) for f in vrx_freqs]

        if len(outs) == 1:
            self.connect(outs[0], audio.sink(AUDIO_RATE, "", True))
        elif len(outs) == 2:
            snk = audio.sink(AUDIO_RATE, "", True)   # stereo: VRX0=L, VRX1=R
            self.connect(outs[0], (snk, 0))
            self.connect(outs[1], (snk, 1))
        else:
            mix = blocks.add_ff(1)
            scale = blocks.multiply_const_ff(1.0 / len(outs))
            for i, o in enumerate(outs):
                self.connect(o, (mix, i))
            self.connect(mix, scale, audio.sink(AUDIO_RATE, "", True))


def main():
    p = argparse.ArgumentParser(description="Multiple VRX in one band (single tuner)")
    p.add_argument("--center", type=float, default=7.15e6,
                   help="tuner centre frequency, Hz (the captured band)")
    p.add_argument("--vrx", type=float, action="append", default=None,
                   help="a VRX frequency, Hz (repeatable). Default: one at --center")
    p.add_argument("--ssb", choices=["lsb", "usb"], default="lsb")
    p.add_argument("--bw", type=int, default=2400, help="SSB audio bandwidth, Hz")
    p.add_argument("--rf-gr", type=int, default=0, help="RF gain reduction dB")
    p.add_argument("--if-gr", type=int, default=40, help="IF gain reduction dB")
    agc_grp = p.add_mutually_exclusive_group()
    agc_grp.add_argument("--agc", dest="agc", action="store_true", default=True)
    agc_grp.add_argument("--no-agc", dest="agc", action="store_false")
    args = p.parse_args()

    vrx = args.vrx if args.vrx else [args.center]
    for f in vrx:
        if abs(f - args.center) > MAX_OFFSET:
            sys.exit(f"VRX {f/1e6:.4f} MHz is outside +/-{MAX_OFFSET/1e6:.2f} MHz "
                     f"of centre {args.center/1e6:.4f} MHz")

    tb = MultiVRX(args.center, vrx, args.ssb, args.bw, args.agc, args.if_gr, args.rf_gr)
    labels = ", ".join(f"{f/1e6:.4f}" for f in vrx)
    print(f"{len(vrx)} VRX @ [{labels}] MHz ({args.ssb.upper()}), "
          f"centre {args.center/1e6:.3f} MHz. Ctrl-C to stop.")
    tb.start()

    def stop(*_):
        tb.stop(); tb.wait(); sys.exit(0)
    signal.signal(signal.SIGINT, stop)
    tb.wait()


if __name__ == "__main__":
    main()
