#!/usr/bin/env python3
"""Unit test for the diversity null (NullEstimator + DiversityCombiner null mode).

No hardware needed. Builds two coherent branches carrying a STRONG interferer
and a WEAKER wanted signal with *different* spatial signatures (h0/h1), plus
noise. Drives the actual server blocks (sharing a NullState) block-by-block in
lockstep — the estimator measures the cancelling weight, the combiner applies
it — and checks:

  (1) the estimated weight converges to h0_i/h1_i (the interferer's ratio),
  (2) the interferer is deeply cancelled in the output,
  (3) the wanted signal (different signature) survives,
  (4) the reported null_depth_db reflects the achieved cancellation.

A second scenario simulates a distant *fading* source (branch-B carrier dips
into the noise) and checks the fade-gating keeps the weight bounded instead of
blowing up as the denominator collapses.

Run:  C:\\Users\\MABY\\radioconda\\python.exe server\\python\\headless\\test_null.py
"""
import numpy as np

from server import NullState, NullEstimator, DiversityCombiner, NULL_MAX_W


def db(x):
    return 10.0 * np.log10(x)


def test_stable():
    """Stable local-style interferer: deep null, wanted signal preserved."""
    print("── stable source ──")
    rng = np.random.default_rng(3)
    N = 200_000
    blk = 4096

    # Interferer (strong) and wanted signal (weaker), each with its own branch
    # ratio. The null should lock onto the interferer's ratio and leave the
    # wanted signal — which arrives with a different ratio — intact.
    h0_i, h1_i = 1.0 + 0j, 0.80 * np.exp(1j * 1.2)     # interferer signature
    h0_s, h1_s = 1.0 + 0j, 0.90 * np.exp(-1j * 0.5)    # wanted signature
    w_true = h0_i / h1_i

    i = (rng.standard_normal(N) + 1j * rng.standard_normal(N)) / np.sqrt(2)      # power 1
    s = 0.10 * (rng.standard_normal(N) + 1j * rng.standard_normal(N)) / np.sqrt(2)  # -20 dB
    n0 = 0.02 * (rng.standard_normal(N) + 1j * rng.standard_normal(N))
    n1 = 0.02 * (rng.standard_normal(N) + 1j * rng.standard_normal(N))
    ni0 = 0.02 * (rng.standard_normal(N) + 1j * rng.standard_normal(N))
    ni1 = 0.02 * (rng.standard_normal(N) + 1j * rng.standard_normal(N))

    # Full band (what the COMBINER sees): interferer + off-frequency wanted + noise.
    x0 = (h0_i * i + h0_s * s + n0).astype(np.complex64)
    x1 = (h1_i * i + h1_s * s + n1).astype(np.complex64)
    # Band-isolated interferer (what the ESTIMATOR sees via the freq-xlate tap):
    # the wanted signal is at a different frequency, so it is NOT in this band.
    xi0 = (h0_i * i + ni0).astype(np.complex64)
    xi1 = (h1_i * i + ni1).astype(np.complex64)

    state = NullState()
    state.active = True
    state.track = True
    est = NullEstimator(state)              # alpha from state (default "med" = 0.05)
    comb = DiversityCombiner(state)

    out = np.empty(N, dtype=np.complex64)
    for k in range(0, N - blk, blk):
        sl = slice(k, k + blk)
        est.work([xi0[sl], xi1[sl]], [])                     # target band -> updates state.w / depth
        comb.work([x0[sl], x1[sl]], [out[sl]])               # full band -> applies state.w

    # Evaluate on the settled second half only.
    half = N // 2
    w = state.w
    interf_in = np.var(h0_i * i[half:])
    # Cancellation of each pure component under the converged weight:
    interf_res = np.var((h0_i - w * h1_i) * i[half:])
    wanted_in = np.var(h0_s * s[half:])
    wanted_res = np.var((h0_s - w * h1_s) * s[half:])

    interf_null_db = db(interf_in / (interf_res + 1e-30))
    wanted_keep_db = db(wanted_res / (wanted_in + 1e-30))
    w_err = abs(w - w_true)

    print(f"est w = {abs(w):.3f} @ {np.degrees(np.angle(w)):+.1f} deg "
          f"(true {abs(w_true):.3f} @ {np.degrees(np.angle(w_true)):+.1f} deg), |err|={w_err:.4f}")
    print(f"interferer cancelled by {interf_null_db:5.1f} dB")
    print(f"wanted signal retained  {wanted_keep_db:+5.1f} dB (0 = untouched)")
    print(f"reported null_depth_db  {state.depth_db:5.1f} dB")

    ok = (w_err < 0.05
          and interf_null_db > 30.0            # deep null on the target
          and wanted_keep_db > -6.0            # wanted signal not nulled away
          and state.depth_db > 20.0)           # server's own metric agrees
    print("  RESULT:", "PASS" if ok else "FAIL")
    return ok


def test_fade():
    """Fading source: branch-B carrier dips into the noise. The fade-gating +
    denominator floor + |w| cap must keep the weight bounded — without them the
    ratio blows up (h1 -> 0) and the null thrashes, which is what wandered the
    amp/phase sliders wildly on a real skywave AM station."""
    print("── fading source ──")
    rng = np.random.default_rng(7)
    N = 300_000
    blk = 4096
    h0_i, h1_i = 1.0 + 0j, 0.80 * np.exp(1j * 1.2)
    w_true = h0_i / h1_i

    i = (rng.standard_normal(N) + 1j * rng.standard_normal(N)) / np.sqrt(2)
    # Slow fade envelope on BRANCH B only (independent fading — the diversity case),
    # dipping to ~1% amplitude (deep fade) a few times across the run.
    t = np.arange(N)
    env = 0.5 * (1 + np.cos(2 * np.pi * t / 40_000)) ** 2      # in [0,2], deep periodic dips
    env = np.clip(env, 0.01, None)
    ni0 = 0.02 * (rng.standard_normal(N) + 1j * rng.standard_normal(N))
    ni1 = 0.02 * (rng.standard_normal(N) + 1j * rng.standard_normal(N))
    xi0 = (h0_i * i + ni0).astype(np.complex64)
    xi1 = (h1_i * env * i + ni1).astype(np.complex64)         # branch B fades

    state = NullState()
    state.active = True
    state.track = True
    est = NullEstimator(state)

    w_mags = []
    for k in range(0, N - blk, blk):
        sl = slice(k, k + blk)
        est.work([xi0[sl], xi1[sl]], [])
        w_mags.append(abs(state.w))
    w_mags = np.array(w_mags)
    peak = float(w_mags.max())

    phase_err = abs(np.angle(state.w) - np.angle(w_true))
    print(f"est w = {abs(state.w):.3f} @ {np.degrees(np.angle(state.w)):+.1f} deg "
          f"(true phase {np.degrees(np.angle(w_true)):+.1f} deg, err {np.degrees(phase_err):.1f} deg)")
    print(f"peak |w| across run = {peak:.2f} (cap {NULL_MAX_W}); "
          f"final band depth {state.depth_db:.1f} dB")

    # The robustness property under fading: the weight stays BOUNDED (never blows
    # up as branch-B collapses) and stays PHASE-locked to the source. Magnitude is
    # deliberately not asserted — a least-squares weight is amplitude-biased under
    # deep AM, which is fine; the phase is what steers the null.
    ok = (peak <= NULL_MAX_W + 1e-6                 # bounded — no blow-up
          and np.degrees(phase_err) < 10.0          # phase locked to the source
          and 0.3 < abs(state.w) < NULL_MAX_W)      # sane magnitude, not collapsed/railed
    print("  RESULT:", "PASS" if ok else "FAIL")
    return ok


def main():
    results = [test_stable(), test_fade()]
    print("\nOVERALL:", "PASS" if all(results) else "FAIL")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
