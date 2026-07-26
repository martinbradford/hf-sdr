#!/usr/bin/env python3
"""
Stage 3 — Diversity Reception (PoC).

    RSPduo dual-tuner (diversity) source ─┬─ stream 0 (Antenna A) ─┐
                                          └─ stream 1 (Antenna B) ─┤
        DiversityCombiner  (phase/amplitude align + MRC)  ─────────┘
            → tune → sideband filter → complex_to_real → AGC
              → resample → audio out

The two tuners in "Dual Tuner (diversity reception)" mode share the RSPduo's
clock/ADC, so the streams are phase-coherent. The combiner estimates the
complex channel ratio g = h1/h0 between the branches and forms the MRC output
y = x0 + conj(g)*x1, which adds the wanted signal coherently while averaging
uncorrelated noise — the diversity gain.

The combiner algorithm is validated against synthetic coherent branches (phase
error < 0.001 rad, combined SNR within ~0.15 dB of ideal MRC). Real combining
gain needs both antennas connected with a signal on each.

Requires: radioconda + gr-sdrplay3, SDRPlay API running, and the device FREE
(close SDRConnect / SDRuno first). Run from a radioconda prompt:

    python diversity_rx.py --freq 7.150e6

NOTE on bring-up: dual-tuner init via gr-sdrplay3 on Windows + API 3.15 can be
fiddly (see gr-sdrplay3 issues #48, #54). If sdrplay_api_Init() fails, launch
and close SDRConnect once to reset the API state, then retry.
"""

import argparse
import signal
import sys
import time

import numpy as np
from gnuradio import gr, blocks, analog, audio
from gnuradio import filter as gr_filter
from gnuradio.filter import firdes

try:
    from gnuradio import sdrplay3
except ImportError:
    sys.exit("gr-sdrplay3 not found. See docs/SETUP_NOTES.md.")

SOURCE_RATE = 2_000_000      # RSPduo dual-tuner ADC/output rate (fixed)
DECIM = 40                   # -> 50 kHz intermediate rate
INTER_RATE = SOURCE_RATE // DECIM
AUDIO_RATE = 48_000


class DiversityCombiner(gr.sync_block):
    """Phase/amplitude-aligned MRC combiner for two coherent branches.

    Model: x0 = h0*s + n0, x1 = h1*s + n1, equal branch noise power.
    Estimate g = h1/h0 = <x1 conj(x0)> / <|x0|^2> (leaky integrator), then
    output MRC combination y = x0 + conj(g)*x1. Logs the estimated correction
    (|g|, angle) and branch/combined powers periodically.
    """

    def __init__(self, alpha=5e-3, log_every=1.0):
        gr.sync_block.__init__(self, name="diversity_combiner",
                               in_sig=[np.complex64, np.complex64],
                               out_sig=[np.complex64])
        self.alpha = alpha
        self.g_num = 0 + 0j
        self.g_den = 1e-12
        self.g = 0 + 0j
        self._log_every = log_every
        self._last_log = 0.0

    def work(self, input_items, output_items):
        x0 = input_items[0]
        x1 = input_items[1]
        a = self.alpha
        self.g_num = (1 - a) * self.g_num + a * np.mean(x1 * np.conj(x0))
        self.g_den = (1 - a) * self.g_den + a * np.mean((x0 * np.conj(x0)).real)
        self.g = self.g_num / self.g_den
        y = x0 + np.conj(self.g) * x1
        output_items[0][:] = y

        now = time.monotonic()
        if now - self._last_log >= self._log_every:
            self._last_log = now
            p0 = np.mean(np.abs(x0) ** 2)
            p1 = np.mean(np.abs(x1) ** 2)
            py = np.mean(np.abs(y) ** 2)
            print(f"correction |g|={abs(self.g):.3f} @ {np.degrees(np.angle(self.g)):+7.1f} deg | "
                  f"P0={10*np.log10(p0+1e-30):6.1f} P1={10*np.log10(p1+1e-30):6.1f} "
                  f"Py={10*np.log10(py+1e-30):6.1f} dBFS", flush=True)
        return len(y)


class DiversityReceiver(gr.top_block):
    def __init__(self, freq, mode, agc, if_gr, rf_gr, audio_on, bw):
        gr.top_block.__init__(self, "HF SDR — Stage 3 Diversity")

        # ---- Dual-tuner diversity source (2 coherent streams) ------------
        self.src = sdrplay3.rspduo(
            "", rspduo_mode="Dual Tuner (diversity reception)", antenna="Both Tuners",
            stream_args=sdrplay3.stream_args(output_type="fc32", channels_size=2))
        self.src.set_sample_rate(SOURCE_RATE)       # fed the dual-tuner ADC rate
        self.src.set_center_freq(freq)              # single-form: both tuners same freq
        self.src.set_bandwidth(1_536_000)           # dual-tuner is Low-IF, fixed BW
        # Diversity LINKS the two tuners: use SINGLE-form setters — they apply
        # to both. The per-tuner-by-index forms set_gain(gr,name,tuner) /
        # set_gain_mode(agc,tuner) and set_center_freq(fA,fB) are independent-RX
        # only ("device is not in independent RX mode") — the freq one segfaults.
        self.src.set_gain_mode(agc)
        if agc:
            self.src.set_agc_setpoint(-30)
        else:
            self.src.set_gain(if_gr, "IF")
        self.src.set_gain(rf_gr, "RF")
        self.src.set_dc_offset_mode(True)
        self.src.set_iq_balance_mode(True)

        # ---- Diversity combiner ------------------------------------------
        self.combiner = DiversityCombiner()

        # ---- Demod chain on the combined stream --------------------------
        xlate_taps = firdes.low_pass(1.0, SOURCE_RATE, 15_000, 5_000)
        self.xlate = gr_filter.freq_xlating_fir_filter_ccf(DECIM, xlate_taps, 0.0, SOURCE_RATE)
        lo_edge = 300                       # high-pass edge: cut carrier/rumble
        low, high = (-bw, -lo_edge) if mode == "lsb" else (lo_edge, bw)
        self.sideband = gr_filter.fir_filter_ccc(
            1, firdes.complex_band_pass(1.0, INTER_RATE, low, high, 200))
        self.to_real = blocks.complex_to_real(1)
        self.agc = analog.agc2_ff(1e-1, 1e-2, 0.3, 1.0)
        # Cap AGC gain so quiet passages don't get amplified into loud hiss.
        self.agc.set_max_gain(1_024)
        self.resamp = gr_filter.rational_resampler_fff(
            interpolation=AUDIO_RATE // 1_000, decimation=INTER_RATE // 1_000)
        self.vol = blocks.multiply_const_ff(0.5)

        # ---- Wire it up --------------------------------------------------
        self.connect((self.src, 0), (self.combiner, 0))
        self.connect((self.src, 1), (self.combiner, 1))
        self.connect(self.combiner, self.xlate, self.sideband, self.to_real,
                     self.agc, self.resamp, self.vol)
        if audio_on:
            self.connect(self.vol, audio.sink(AUDIO_RATE, "", True))
        else:
            self.connect(self.vol, blocks.null_sink(gr.sizeof_float))


def main():
    p = argparse.ArgumentParser(description="Stage 3 diversity receiver PoC")
    p.add_argument("--freq", type=float, default=7.15e6, help="tuned frequency, Hz")
    p.add_argument("--mode", choices=["lsb", "usb"], default="lsb", help="sideband")
    p.add_argument("--bw", type=int, default=2400,
                   help="SSB audio bandwidth / high cutoff, Hz (lower to cut hiss)")
    p.add_argument("--rf-gr", type=int, default=0, help="RF gain reduction dB")
    p.add_argument("--if-gr", type=int, default=40, help="IF gain reduction dB [20-59]")
    p.add_argument("--no-audio", dest="audio", action="store_false", default=True,
                   help="metrics only, no audio output")
    agc_grp = p.add_mutually_exclusive_group()
    agc_grp.add_argument("--agc", dest="agc", action="store_true", default=True)
    agc_grp.add_argument("--no-agc", dest="agc", action="store_false")
    args = p.parse_args()

    tb = DiversityReceiver(args.freq, args.mode, args.agc, args.if_gr, args.rf_gr,
                           args.audio, args.bw)
    print(f"Diversity RX @ {args.freq/1e6:.4f} MHz ({args.mode.upper()}). "
          f"Watch |g| and phase settle (coherent streams => stable). Ctrl-C to stop.")
    tb.start()

    def stop(*_):
        tb.stop(); tb.wait(); sys.exit(0)
    signal.signal(signal.SIGINT, stop)
    tb.wait()

    # Reached only if the flowgraph ended on its own (Ctrl-C sys.exits above).
    print("\n[!] Stopped with no data — the RSPduo dual-tuner init likely failed.\n"
          "    Reset the SDRplay API: open & close SDRConnect once, then retry.",
          file=sys.stderr)


if __name__ == "__main__":
    main()
