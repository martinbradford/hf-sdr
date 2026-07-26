#!/usr/bin/env python3
"""Unit test for DiversityCombiner using synthetic coherent branches.

No hardware needed. Generates x0 = h0*s + n0, x1 = h1*s + n1 with a known
channel offset and independent noise, runs them through the actual gr.sync_block,
and checks: (1) the phase alignment is accurate, (2) the MRC output beats the
best branch and approaches ideal MRC.

Run:  python test_combiner.py
"""
import numpy as np
from gnuradio import gr, blocks

from diversity_rx import DiversityCombiner


def db(x):
    return 10 * np.log10(x)


def main():
    rng = np.random.default_rng(1)
    N = 400_000
    h0 = 1.0 + 0j
    h1 = 0.7 * np.exp(1j * 2.0)          # branch 1: 0.7x amplitude, +2.0 rad
    noise_pow = 0.5                       # per-branch noise power (~3 dB / -0.1 dB SNR)

    s = (rng.standard_normal(N) + 1j * rng.standard_normal(N)) / np.sqrt(2)
    n0 = np.sqrt(noise_pow / 2) * (rng.standard_normal(N) + 1j * rng.standard_normal(N))
    n1 = np.sqrt(noise_pow / 2) * (rng.standard_normal(N) + 1j * rng.standard_normal(N))
    x0 = (h0 * s + n0).astype(np.complex64)
    x1 = (h1 * s + n1).astype(np.complex64)

    tb = gr.top_block()
    comb = DiversityCombiner(alpha=5e-3, log_every=1e9)   # suppress periodic logging
    snk = blocks.vector_sink_c()
    tb.connect(blocks.vector_source_c(x0.tolist(), False), (comb, 0))
    tb.connect(blocks.vector_source_c(x1.tolist(), False), (comb, 1))
    tb.connect(comb, snk)
    tb.run()

    g = comb.g
    snr0 = db((abs(h0) ** 2) / noise_pow)
    snr1 = db((abs(h1) ** 2) / noise_pow)
    sig_out = h0 * s + np.conj(g) * h1 * s
    noi_out = n0 + np.conj(g) * n1
    snr_out = db(np.var(sig_out) / np.var(noi_out))
    mrc_ideal = db(10 ** (snr0 / 10) + 10 ** (snr1 / 10))
    phase_err = abs(np.angle(g) - np.angle(h1 / h0))

    print(f"est g = {abs(g):.3f} @ {np.degrees(np.angle(g)):+.1f} deg "
          f"(true phase {np.degrees(np.angle(h1/h0)):+.1f} deg)")
    print(f"branch SNRs: {snr0:.2f} / {snr1:.2f} dB   "
          f"combined {snr_out:.2f} dB (ideal MRC {mrc_ideal:.2f})")
    print(f"phase error {phase_err:.4f} rad, gain over best branch {snr_out-max(snr0,snr1):+.2f} dB")

    ok = (phase_err < 0.05 and snr_out > max(snr0, snr1) + 1.0
          and snr_out > mrc_ideal - 0.5)
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
