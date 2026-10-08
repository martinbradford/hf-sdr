#!/usr/bin/env python3
"""Does an in-place sideband change keep the audio flowing? (needs GNU Radio, NOT the RSP)

Builds the real VRX demod chain (SdrServer._make_vrx) on a synthetic 2 MS/s input holding two
tones relative to the VRX frequency:

    +1000 Hz  -> inside the USB passband, outside LSB   (audio would be 1000 Hz in USB)
    -2000 Hz  -> inside the LSB passband, outside USB   (audio would be 2000 Hz in LSB)

It runs in USB, then calls SdrServer.update_vrx(mode="lsb") mid-stream, then switches back, and
checks from the audio the chain actually emits:

  (1) the audio follows the sideband: 1000 Hz dominates in USB, 2000 Hz in LSB, 1000 Hz again,
  (2) there is no gap: the longest pause between audio frames around each switch stays far below
      the multi-second gap of the old remove_vrx + add_vrx path (reported, with a limit),
  (3) the audio sequence numbers stay contiguous and the VRX id never changes.

It also reports the size of the click at each switch (largest sample-to-sample jump in a window
around the switch, relative to normal), so the transient can be judged: this is the number to
look at if the swap sounds objectionable.

Run on a machine with GNU Radio (e.g. the shack PC):
    C:\\Users\\MABY\\radioconda\\python.exe server\\python\\headless\\test_sideband_flowgraph.py
Takes ~10 s. Does not touch the RSP, so it can run while the server is stopped.
"""
import sys
import time
import types

import numpy as np

try:
    from gnuradio import analog, blocks, gr
except ImportError:
    sys.exit("This test needs GNU Radio (it builds a real flowgraph). Run it on the shack PC.")

import server  # noqa: E402  (needs gr-sdrplay3 importable, as the server does)
from server import AUDIO_RATE, SOURCE_RATE  # noqa: E402

CENTER = 7_150_000
TONE_USB_HZ, TONE_LSB_HZ = +1000, -2000
PHASE_S = 1.6                 # time spent in each mode
MAX_GAP_S = 0.25              # pass limit for the longest pause between audio frames
CLICK_WINDOW_S = 0.05


class FakePub:
    """Collects audio frames the way AudioSink hands them to the Publisher."""
    def __init__(self):
        self.frames = []      # (t_monotonic, vrx_id, seq, np.int16 array)

    def send(self, sock, topic, header, payload=b""):
        if sock == "audio":
            self.frames.append((time.monotonic(), header["vrx_id"], header["seq"],
                                np.frombuffer(payload, dtype="<i2").copy()))


def dominant_hz(x):
    x = x.astype(np.float64) * np.hanning(len(x))
    spec = np.abs(np.fft.rfft(x))
    return np.fft.rfftfreq(len(x), 1 / AUDIO_RATE)[int(np.argmax(spec))], spec


def window_audio(frames, t0, t1):
    parts = [f[3] for f in frames if t0 <= f[0] < t1]
    return np.concatenate(parts) if parts else np.zeros(0, dtype=np.int16)


def main():
    pub = FakePub()
    fake = types.SimpleNamespace(
        pub=pub, center=CENTER, _vrx={}, _next_id=1, _audio_on=True,
        _lp_taps=server.firdes.low_pass(1.0, SOURCE_RATE, 15_000, 5_000))
    fake._vrx_public = lambda vid: server.SdrServer._vrx_public(fake, vid)

    rec = server.SdrServer._make_vrx(fake, CENTER, mode="usb", volume=0.5)
    fake._vrx[rec["vrx_id"]] = rec
    vid = rec["vrx_id"]

    tb = gr.top_block()
    a = analog.sig_source_c(SOURCE_RATE, analog.GR_COS_WAVE, TONE_USB_HZ, 0.2)
    b = analog.sig_source_c(SOURCE_RATE, analog.GR_COS_WAVE, TONE_LSB_HZ, 0.2)
    add = blocks.add_cc()
    thr = blocks.throttle(gr.sizeof_gr_complex, SOURCE_RATE)      # real-time pacing
    tb.connect(a, (add, 0)); tb.connect(b, (add, 1)); tb.connect(add, thr)
    tb.connect(thr, rec["chain"][0])
    for x, y in zip(rec["chain"], rec["chain"][1:]):
        tb.connect(x, y)

    tb.start()
    t_start = time.monotonic()
    time.sleep(PHASE_S)
    t_sw1 = time.monotonic()
    out1 = server.SdrServer.update_vrx(fake, vid, mode="lsb")
    time.sleep(PHASE_S)
    t_sw2 = time.monotonic()
    out2 = server.SdrServer.update_vrx(fake, vid, mode="usb")
    time.sleep(PHASE_S)
    t_end = time.monotonic()
    tb.stop(); tb.wait()

    frames = pub.frames
    fails = 0

    def check(cond, msg):
        nonlocal fails
        print(("ok   " if cond else "FAIL ") + msg)
        fails += 0 if cond else 1

    check(out1["mode"] == "lsb" and out2["mode"] == "usb", "update_vrx reported the new modes")
    check(len(frames) > 10, f"audio frames received ({len(frames)})")
    if len(frames) <= 10:
        sys.exit("no audio produced; cannot continue")

    # (1) audio follows the sideband. Skip 0.7 s after start/switch: GNU Radio's inter-block buffers
    # delay the audio by a few hundred ms, plus AGC + filter settling.
    settle = 0.7
    for name, t0, t1, want, other in (
            ("USB", t_start + settle, t_sw1, TONE_USB_HZ, abs(TONE_LSB_HZ)),
            ("LSB", t_sw1 + settle, t_sw2, abs(TONE_LSB_HZ), TONE_USB_HZ),
            ("USB again", t_sw2 + settle, t_end, TONE_USB_HZ, abs(TONE_LSB_HZ))):
        x = window_audio(frames, t0, t1)
        if len(x) < 4096:
            check(False, f"{name}: too little audio to analyse ({len(x)} samples)")
            continue
        hz, spec = dominant_hz(x[:8192] if len(x) >= 8192 else x)
        freqs = np.fft.rfftfreq(min(len(x), 8192), 1 / AUDIO_RATE)
        e_want = spec[np.argmin(np.abs(freqs - want))]
        e_other = spec[np.argmin(np.abs(freqs - other))]
        check(abs(hz - want) < 40 and e_want > 10 * e_other,
              f"{name}: dominant {hz:.0f} Hz (want {want}); wanted/unwanted tone = {e_want / max(e_other, 1e-9):.0f}x")

    # (2) no gap around the switches
    ts = np.array([f[0] for f in frames])
    gaps = np.diff(ts)
    for name, tsw in (("USB->LSB", t_sw1), ("LSB->USB", t_sw2)):
        near = gaps[(ts[1:] > tsw - 0.2) & (ts[1:] < tsw + 0.5)]
        worst = float(near.max()) if len(near) else float("nan")
        check(worst < MAX_GAP_S, f"{name}: longest pause between audio frames {worst * 1000:.0f} ms (limit {MAX_GAP_S * 1000:.0f} ms)")
    print(f"     (whole run: longest pause {gaps.max() * 1000:.0f} ms, median {np.median(gaps) * 1000:.0f} ms)")

    # (3) one VRX, contiguous sequence
    ids = {f[1] for f in frames}
    seqs = [f[2] for f in frames]
    check(ids == {vid}, "the VRX id never changed")
    check(all(b - a == 1 for a, b in zip(seqs, seqs[1:])), "audio sequence numbers are contiguous across both switches")

    # click size at each switch, for judging the transient
    full = np.concatenate([f[3] for f in frames]).astype(np.float64)
    typical = float(np.percentile(np.abs(np.diff(full)), 99))
    for name, tsw in (("USB->LSB", t_sw1), ("LSB->USB", t_sw2)):
        w = window_audio(frames, tsw - CLICK_WINDOW_S, tsw + CLICK_WINDOW_S).astype(np.float64)
        jump = float(np.abs(np.diff(w)).max()) if len(w) > 1 else float("nan")
        print(f"     {name}: largest sample jump near the switch {jump:.0f} vs typical (99th pct) {typical:.0f}"
              f"  ({jump / max(typical, 1):.1f}x)")

    print("ALL PASS" if not fails else f"{fails} FAILED")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
