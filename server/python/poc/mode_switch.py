#!/usr/bin/env python3
"""
RSPduo mode experiment — single / diversity / independent (twin) tuner.

A thin HAL: `build_source(mode, ...)` returns a correctly-configured RSPduo
source for the chosen mode, encapsulating the mode-specific rules learned
during bring-up (see docs/SETUP_NOTES.md). The rest of the flowgraph adapts:

    single       : Tuner 1 -> demod -> audio (mono)
    diversity    : Tuner 1 + Tuner 2 (same freq) -> combiner -> demod -> audio
    independent  : Tuner 1 @ freq, Tuner 2 @ freq-b -> two demods -> stereo
                   (A = left, B = right)

Mode is a construction-time property of the RSPduo, so "switching" means
relaunching with a different --mode (a live in-app switch would tear down and
rebuild the flowgraph — a planned follow-up). Examples:

    python mode_switch.py --mode single    --freq 7.150e6
    python mode_switch.py --mode diversity --freq 7.150e6 --rf-gr 30
    python mode_switch.py --mode independent --freq 7.150e6 --freq-b 14.074e6 --ssb usb

NOTE: dual-tuner (diversity/independent) init via gr-sdrplay3 can fail with
sdrplay_api_Fail on Windows/API 3.15 — launch & close SDRConnect once to reset
the API state, then retry (see SETUP_NOTES).
"""

import argparse
import signal
import sys

from gnuradio import gr, blocks, analog, audio
from gnuradio import filter as gr_filter
from gnuradio.filter import firdes

from diversity_rx import DiversityCombiner

try:
    from gnuradio import sdrplay3
except ImportError:
    sys.exit("gr-sdrplay3 not found. See docs/SETUP_NOTES.md.")

SOURCE_RATE = 2_000_000
DECIM = 40
INTER_RATE = SOURCE_RATE // DECIM
AUDIO_RATE = 48_000


def _apply_gain(src, agc, if_gr, rf_gr, tuner=None):
    """Set gain/AGC. Single-form (tuner=None) for single & diversity (linked);
    per-tuner (tuner=0/1) only for independent RX mode."""
    if tuner is None:
        src.set_gain_mode(agc)
        if agc:
            src.set_agc_setpoint(-30)
        else:
            src.set_gain(if_gr, "IF")
        src.set_gain(rf_gr, "RF")
    else:
        src.set_gain_mode(agc, tuner)
        if not agc:
            src.set_gain(if_gr, "IF", tuner)
        src.set_gain(rf_gr, "RF", tuner)


def build_source(mode, freq_a, freq_b, agc, if_gr, rf_gr):
    """HAL: return (source_block, n_channels) configured for `mode`."""
    if mode == "single":
        src = sdrplay3.rspduo(
            "", rspduo_mode="Single Tuner", antenna="Tuner 1 50 ohm",
            stream_args=sdrplay3.stream_args(output_type="fc32", channels_size=1))
        src.set_sample_rate(SOURCE_RATE)
        src.set_center_freq(freq_a)
        src.set_bandwidth(1_536_000)
        _apply_gain(src, agc, if_gr, rf_gr)
        nchan = 1

    elif mode == "diversity":
        src = sdrplay3.rspduo(
            "", rspduo_mode="Dual Tuner (diversity reception)", antenna="Both Tuners",
            stream_args=sdrplay3.stream_args(output_type="fc32", channels_size=2))
        src.set_sample_rate(SOURCE_RATE)
        src.set_center_freq(freq_a)              # single-form: both tuners locked
        src.set_bandwidth(1_536_000)
        _apply_gain(src, agc, if_gr, rf_gr)      # single-form (linked tuners)
        nchan = 2

    elif mode == "independent":
        src = sdrplay3.rspduo(
            "", rspduo_mode="Dual Tuner (independent RX)", antenna="Both Tuners",
            stream_args=sdrplay3.stream_args(output_type="fc32", channels_size=2))
        src.set_sample_rate(SOURCE_RATE)
        src.set_center_freq(freq_a, freq_b)      # (A, B) form valid ONLY here
        src.set_bandwidth(1_536_000)
        for tuner in (0, 1):                     # per-tuner gain valid here
            _apply_gain(src, agc, if_gr, rf_gr, tuner=tuner)
        if agc:
            src.set_agc_setpoint(-30)            # else IF AGC targets too high -> constant overload
        nchan = 2

    else:
        raise ValueError(f"unknown mode {mode}")

    src.set_dc_offset_mode(True)
    src.set_iq_balance_mode(True)
    return src, nchan


def add_demod(tb, block, port, ssb, bw):
    """Attach an SSB demod chain to (block, port); return its float output."""
    xlate = gr_filter.freq_xlating_fir_filter_ccf(
        DECIM, firdes.low_pass(1.0, SOURCE_RATE, 15_000, 5_000), 0.0, SOURCE_RATE)
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


class Receiver(gr.top_block):
    def __init__(self, mode, freq_a, freq_b, ssb, bw, agc, if_gr, rf_gr):
        gr.top_block.__init__(self, f"HF SDR — {mode} mode")
        src, nchan = build_source(mode, freq_a, freq_b, agc, if_gr, rf_gr)

        if mode == "diversity":
            comb = DiversityCombiner()
            self.connect((src, 0), (comb, 0))
            self.connect((src, 1), (comb, 1))
            out = add_demod(self, comb, 0, ssb, bw)
            self.connect(out, audio.sink(AUDIO_RATE, "", True))
        elif mode == "independent":
            left = add_demod(self, src, 0, ssb, bw)
            right = add_demod(self, src, 1, ssb, bw)
            snk = audio.sink(AUDIO_RATE, "", True)   # stereo: A=left, B=right
            self.connect(left, (snk, 0))
            self.connect(right, (snk, 1))
        else:  # single
            out = add_demod(self, src, 0, ssb, bw)
            self.connect(out, audio.sink(AUDIO_RATE, "", True))


def main():
    p = argparse.ArgumentParser(description="RSPduo mode experiment")
    p.add_argument("--mode", choices=["single", "diversity", "independent"],
                   default="single")
    p.add_argument("--freq", type=float, default=7.15e6, help="tuner 1 frequency, Hz")
    p.add_argument("--freq-b", type=float, default=14.074e6,
                   help="tuner 2 frequency, Hz (independent mode only)")
    p.add_argument("--ssb", choices=["lsb", "usb"], default="lsb")
    p.add_argument("--bw", type=int, default=2400, help="SSB audio bandwidth, Hz")
    p.add_argument("--rf-gr", type=int, default=0, help="RF gain reduction dB")
    p.add_argument("--if-gr", type=int, default=40, help="IF gain reduction dB")
    agc_grp = p.add_mutually_exclusive_group()
    agc_grp.add_argument("--agc", dest="agc", action="store_true", default=True)
    agc_grp.add_argument("--no-agc", dest="agc", action="store_false")
    args = p.parse_args()

    tb = Receiver(args.mode, args.freq, args.freq_b, args.ssb, args.bw,
                  args.agc, args.if_gr, args.rf_gr)
    if args.mode == "independent":
        print(f"independent: L={args.freq/1e6:.4f} MHz  R={args.freq_b/1e6:.4f} MHz "
              f"({args.ssb.upper()}). Ctrl-C to stop.")
    else:
        print(f"{args.mode}: {args.freq/1e6:.4f} MHz ({args.ssb.upper()}). Ctrl-C to stop.")
    tb.start()

    def stop(*_):
        tb.stop(); tb.wait(); sys.exit(0)
    signal.signal(signal.SIGINT, stop)
    tb.wait()

    # Reached only if the flowgraph ended on its own (Ctrl-C sys.exits above).
    if args.mode in ("diversity", "independent"):
        print("\n[!] Stopped with no data — the RSPduo dual-tuner init likely failed.\n"
              "    Reset the SDRplay API: open & close SDRConnect once, then retry.",
              file=sys.stderr)
    else:
        print("\n[!] Source stopped unexpectedly (no samples).", file=sys.stderr)


if __name__ == "__main__":
    main()
