#!/usr/bin/env python3
"""
HF SDR headless server (Stage 4) — implements protocol/messages.md v0.1.

Single-tuner and diversity capture with:
  - a spectrum publisher (stream socket),
  - dynamically add/removable VRX demod chains (audio socket), via lock()/unlock(),
  - a JSON REQ/REP control server,
  - LIVE tuner-mode switching (single <-> diversity) over the control channel:
    the flowgraph is stopped, rebuilt in the new mode, verified, and restarted
    in-process (no server restart, sockets stay bound). Dual-tuner init is
    unreliable (docs/SETUP_NOTES.md), so the switch verifies samples flow and
    retries; on persistent failure it reverts to single and returns device_error.

Independent (dual-tuner, two bands) is not yet implemented here — it needs the
two-capture-window VRX model; those commands return `unsupported` for now.

    python server.py --center 7.15e6
"""

import argparse
import gc
import hmac
import json
import os
import queue
import signal
import sys
import threading
import time

import numpy as np
import zmq
from gnuradio import gr, blocks, analog, fft
from gnuradio import filter as gr_filter
from gnuradio.filter import firdes
from gnuradio.fft import window

try:
    from gnuradio import sdrplay3
except ImportError:
    sys.exit("gr-sdrplay3 not found. See docs/SETUP_NOTES.md.")

PROTOCOL_VERSION = "0.1"
SOURCE_RATE = 2_000_000
VRX_DECIM = 40
INTER_RATE = SOURCE_RATE // VRX_DECIM       # 50 kHz
AUDIO_RATE = 48_000
DISPLAY_DECIM = 8
DISPLAY_RATE = SOURCE_RATE // DISPLAY_DECIM  # 250 kHz span
MAX_VRX = 8
INIT_VERIFY_S = 1.5                          # wait to confirm samples flow
INIT_RETRIES = 3                             # dual-tuner init attempts
PEAK_BLOCK = 4096                            # samples per raw-stream peak-hold block
OVERLOAD_DBFS = -1.0                         # peak at/above this = ADC overload (fc32 full scale = 0 dBFS)
NULL_DECIM = 40                              # -> 50 kHz band-isolation rate for null estimation
NULL_RATE = SOURCE_RATE // NULL_DECIM        # 50 kHz
NULL_WIDTH_DEFAULT = 12_000                  # target estimation bandwidth (Hz) if unspecified
NULL_WIDTH_RANGE = (1_000, 40_000)           # clamp for the target width
NULL_ALPHA = {"fast": 0.15, "med": 0.05, "slow": 0.015}   # tracking-speed -> leaky-integrator alpha
NULL_FADE_FRAC = 0.25                         # block power below this fraction of typical = fade -> hold
NULL_MAX_W = 8.0                              # cap |w| so a fade can't amplify branch-B noise

DEFAULT_FILTERS = {           # audio passband edges (Hz) per mode
    "lsb": (-3000, -200),     # wide enough for data (FT8 ~0-3000 Hz) and voice
    "usb": (200, 3000),
    "cw":  (400, 900),
}
TUNER_MODES = ("single", "diversity")


def _clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


class ProtoError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def bind_or_exit(sock, host, port, name):
    """Bind `sock` to tcp://host:port, or exit non-zero with a message naming the
    port. A failed bind must be fatal: a server that streams but cannot be
    controlled (or vice versa) is worse than none, esp. when launched hidden."""
    try:
        sock.bind(f"tcp://{host}:{port}")
    except zmq.ZMQError as e:
        print(f"hf-sdr-server: cannot bind {name} port {host}:{port} ({e}). "
              f"Is another server already running?", file=sys.stderr, flush=True)
        sys.exit(2)


# --------------------------------------------------------------------------
# Publisher: one thread owns the PUB sockets (zmq sockets are not thread-safe,
# and many GNU Radio work() threads feed them). Sinks enqueue; this drains.
# --------------------------------------------------------------------------
class Publisher:
    def __init__(self, ctx, host, stream_port, audio_port):
        self._stream = ctx.socket(zmq.PUB); bind_or_exit(self._stream, host, stream_port, "stream")
        self._audio = ctx.socket(zmq.PUB); bind_or_exit(self._audio, host, audio_port, "audio")
        self._q = queue.Queue(maxsize=512)
        self._run = True
        threading.Thread(target=self._loop, daemon=True).start()

    def send(self, sock, topic, header, payload=b""):
        try:
            self._q.put_nowait((sock, topic.encode(), json.dumps(header).encode(), payload))
        except queue.Full:
            pass          # real-time: drop rather than block

    def _loop(self):
        while self._run:
            try:
                sock, topic, hdr, payload = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            (self._stream if sock == "stream" else self._audio).send_multipart([topic, hdr, payload])


# --------------------------------------------------------------------------
# Custom blocks
# --------------------------------------------------------------------------
class NullState:
    """Shared handle between the null estimator (which measures the cancelling
    weight in the targeted band) and the combiner (which applies it broadband).

    Nulling model: an interferer arrives as h0*i on branch 0 and h1*i on branch
    1. y = x0 - w*x1 cancels it when w = h0/h1. We estimate that ratio from the
    branches band-limited to the target region (where the interferer dominates),
    so the null locks onto *that* emitter's spatial signature and leaves signals
    elsewhere in the passband intact.
    """
    def __init__(self):
        self.active = False       # null engaged (combiner subtracts w*x1)?
        self.track = True         # keep re-estimating (False = freeze weight)
        self.manual = False       # manual weight override (estimator won't set w)
        self.w = 0 + 0j           # applied cancelling weight  (h0/h1)
        self.r = 0 + 0j           # latest estimate (may differ from w if frozen)
        self.depth_db = 0.0       # measured cancellation in the target band
        self.center_hz = 0        # target centre (absolute Hz)
        self.width_hz = 0         # target estimation bandwidth
        self.speed = "med"        # tracking speed preset
        self.alpha = NULL_ALPHA["med"]   # leaky-integrator rate (set from speed)


class DiversityCombiner(gr.sync_block):
    """Coherent two-branch combiner with two modes.

    MRC (default): maximise combined SNR. g = h1/h0 = <x1 conj(x0)>/<|x0|^2>
    (leaky); y = x0 + conj(g)*x1.

    Null (when null_state.active): cancel a targeted interferer using the weight
    the NullEstimator measures in its band. y = x0 - w*x1.
    """
    def __init__(self, null_state, alpha=5e-3):
        gr.sync_block.__init__(self, "diversity_combiner",
                               [np.complex64, np.complex64], [np.complex64])
        self.alpha = alpha
        self.g_num = 0 + 0j
        self.g_den = 1e-12
        self.g = 0 + 0j
        self._null = null_state

    def work(self, input_items, output_items):
        x0, x1 = input_items[0], input_items[1]
        if self._null.active:
            output_items[0][:] = x0 - self._null.w * x1
            return len(output_items[0])
        a = self.alpha
        self.g_num = (1 - a) * self.g_num + a * np.mean(x1 * np.conj(x0))
        self.g_den = (1 - a) * self.g_den + a * np.mean((x0 * np.conj(x0)).real)
        self.g = self.g_num / self.g_den
        output_items[0][:] = x0 + np.conj(self.g) * x1
        return len(output_items[0])


class NullEstimator(gr.sync_block):
    """Estimates the cancelling weight w = h0/h1 from two band-limited branches.

    Fed the target region of both branches (freq-xlated + decimated so the
    interferer dominates). Leaky-integrates w = <x0 conj(x1)>/<|x1|^2>, and
    measures the achieved null depth as <|x0|^2>/<|x0 - w*x1|^2> in that band.
    A sink (no outputs); writes results into the shared NullState.

    Fade-robust: a distant, fading source dips on branch B independently of
    branch A, which drives the denominator toward the noise floor and makes the
    raw ratio blow up (and thrash). So we (a) skip estimation during a fade —
    hold the last good weight — detected as this block's branch-B power dropping
    well below its typical level; (b) regularise the denominator; and (c) cap
    |w| so a momentary spike can't amplify branch-B noise. Tracking rate (alpha)
    is read live from the NullState so the client can pick Fast/Med/Slow.
    """
    def __init__(self, null_state):
        gr.sync_block.__init__(self, "null_estimator",
                               [np.complex64, np.complex64], [])
        self._s = null_state
        self._num = 0 + 0j        # <x0 conj(x1)>
        self._den = 1e-12         # <|x1|^2>
        self._p0 = 1e-12          # <|x0|^2>  (reference band power)
        self._pe = 1e-12          # <|residual|^2>
        self._ref = 1e-12         # typical branch-B band power (rise-fast/decay-slow) for fade detect

    def work(self, input_items, output_items):
        x0, x1 = input_items[0], input_items[1]
        a = self._s.alpha
        p1 = float(np.mean((x1 * np.conj(x1)).real))
        # Typical branch-B level: jump up to peaks, decay slowly. A fade reads as
        # this block sitting well below it — then we hold rather than divide into noise.
        self._ref = max(self._ref * 0.999, p1)
        if p1 < NULL_FADE_FRAC * self._ref:
            return len(x1)                       # fade: hold weight, depth, everything
        self._num = (1 - a) * self._num + a * np.mean(x0 * np.conj(x1))
        self._den = (1 - a) * self._den + a * p1
        w = self._num / (self._den + 1e-6 * self._ref + 1e-12)   # regularised
        mag = abs(w)
        if mag > NULL_MAX_W:
            w *= NULL_MAX_W / mag                 # cap: never amplify branch-B noise
        self._s.r = w
        if self._s.track and not self._s.manual:
            self._s.w = w
        resid = x0 - self._s.w * x1
        self._p0 = (1 - a) * self._p0 + a * float(np.mean((x0 * np.conj(x0)).real))
        self._pe = (1 - a) * self._pe + a * float(np.mean((resid * np.conj(resid)).real))
        self._s.depth_db = float(10.0 * np.log10(self._p0 / (self._pe + 1e-30)))
        return len(x1)


class SpectrumSink(gr.sync_block):
    """Vector float32[fft_size] -> stream socket, time-throttled to rate_hz."""
    def __init__(self, pub, topic, fft_size, header_fn, enabled_fn, rate_hz=15.0):
        gr.sync_block.__init__(self, "spectrum_sink", [(np.float32, fft_size)], [])
        self._pub, self._topic, self._hdr, self._en = pub, topic, header_fn, enabled_fn
        self._period = 1.0 / rate_hz
        self._last = 0.0
        self._seq = 0
        self.work_count = 0          # every work() call — used to verify init

    def set_rate(self, rate_hz):
        self._period = 1.0 / max(0.1, rate_hz)

    def work(self, input_items, output_items):
        inp = input_items[0]
        self.work_count += 1
        now = time.monotonic()
        if self._en() and now - self._last >= self._period:
            self._last = now
            hdr = self._hdr()
            hdr.update(seq=self._seq, t_utc_ms=int(time.time() * 1000))
            self._seq += 1
            self._pub.send("stream", self._topic, hdr, inp[-1].astype(np.float32).tobytes())
        return len(inp)


class AudioSink(gr.sync_block):
    """Float32 audio -> audio socket as int16 chunks."""
    def __init__(self, pub, vrx_id, rate, enabled_fn):
        gr.sync_block.__init__(self, f"audio_sink_{vrx_id}", [np.float32], [])
        self._pub, self._vid, self._rate, self._en = pub, vrx_id, rate, enabled_fn
        self._seq = 0

    def work(self, input_items, output_items):
        inp = input_items[0]
        if self._en():
            i16 = (np.clip(inp, -1.0, 1.0) * 32767).astype("<i2")
            hdr = {"vrx_id": self._vid, "seq": self._seq, "rate_hz": self._rate,
                   "format": "int16", "channels": 1, "samples": len(inp),
                   "t_utc_ms": int(time.time() * 1000)}
            self._seq += 1
            self._pub.send("audio", f"audio/{self._vid}", hdr, i16.tobytes())
        return len(inp)


# --------------------------------------------------------------------------
# Flowgraph / server state
# --------------------------------------------------------------------------
class SdrServer(gr.top_block):
    def __init__(self, pub, center, fft_size=2048):
        gr.top_block.__init__(self, "hf-sdr-headless")
        self.pub = pub
        self.center = int(center)
        self.fft_size = fft_size
        self.gain = {"agc": True, "if_gr_db": 40, "rf_gr_db": 0, "agc_setpoint_dbfs": -30}
        self._lna_state = 0                # resulting LNA state after the last RF apply
        self._rf_steps = None              # discrete valid RF reductions (dB), enumerated once
        self._peak_probes = []             # per-tuner raw-stream peak-hold probes
        self._vrx = {}
        self._next_id = 1
        self._audio_on = True
        self._spectrum_on = True
        self._lp_taps = firdes.low_pass(1.0, SOURCE_RATE, 15_000, 5_000)
        self._mode = "single"
        self.src = None
        self.combiner = None
        self._demod_src = None            # block VRXs + spectrum tap off (mode-dependent)
        self.spec_sink = None
        self._null = NullState()          # persists across engage/clear
        self._null_chain = None           # [xlate0, xlate1, estimator] while engaged
        self._build(self._mode)

    # ---- flowgraph construction --------------------------------------
    def _build_source(self, mode):
        if mode == "single":
            src = sdrplay3.rspduo(
                "", rspduo_mode="Single Tuner", antenna="Tuner 1 50 ohm",
                stream_args=sdrplay3.stream_args(output_type="fc32", channels_size=1))
        elif mode == "diversity":
            src = sdrplay3.rspduo(
                "", rspduo_mode="Dual Tuner (diversity reception)", antenna="Both Tuners",
                stream_args=sdrplay3.stream_args(output_type="fc32", channels_size=2))
        else:
            raise ProtoError("unsupported", f"tuner mode {mode} not implemented")
        src.set_sample_rate(SOURCE_RATE)
        src.set_center_freq(self.center)       # single-form (both tuners locked in diversity)
        src.set_bandwidth(1_536_000)
        self.src = src
        self._enumerate_rf_steps()             # discover the discrete LNA steps (cached)
        self._apply_gain()                     # single-form gain (both tuners in diversity)
        src.set_dc_offset_mode(True)
        src.set_iq_balance_mode(True)
        return src

    def _enumerate_rf_steps(self):
        """RF gain reduction is a set of discrete, band-limited LNA states (not
        continuous). Sweep the requested range once and record the distinct
        values the driver snaps to, so the client can offer one detent per step
        instead of a coarse continuous slider. Cached (fixed across HF). Runs
        before start()/enable, and restores the configured RF value afterwards."""
        if self._rf_steps is not None:
            return
        lo, hi = self._gr_range("RF")
        seen = set()
        for req in range(int(lo), int(hi) + 1):
            self.src.set_gain(-float(req), "RF")
            seen.add(int(round(-self.src.get_gain("RF"))))
        self._rf_steps = sorted(seen)
        self.src.set_gain(-float(_clamp(self.gain["rf_gr_db"], lo, hi)), "RF")   # restore

    def _build(self, mode):
        """Build source + combiner (if diversity) + spectrum chain. Leaves the
        graph stopped and VRX-less; caller re-adds VRXs and starts."""
        src = self._build_source(mode)
        if mode == "diversity":
            self.combiner = DiversityCombiner(self._null)
            self.connect((src, 0), (self.combiner, 0))
            self.connect((src, 1), (self.combiner, 1))
            self._demod_src = self.combiner
        else:
            self.combiner = None
            self._demod_src = src

        # Peak-hold tap on each raw tuner stream (pre-filter, where ADC clipping
        # shows) -> a per-block max fed to a probe we read when building spectrum
        # headers. fc32 full scale = ADC full scale, so this is real headroom.
        self._peak_probes = []
        for ch in range(2 if mode == "diversity" else 1):
            pmag = blocks.complex_to_mag(1)
            ps2v = blocks.stream_to_vector(gr.sizeof_float, PEAK_BLOCK)
            pmax = blocks.max_ff(PEAK_BLOCK)
            probe = blocks.probe_signal_f()
            self.connect((src, ch), pmag, ps2v, pmax, probe)
            self._peak_probes.append(probe)

        disp_taps = firdes.low_pass(1.0, SOURCE_RATE, DISPLAY_RATE * 0.45, DISPLAY_RATE * 0.10)
        disp = gr_filter.fir_filter_ccf(DISPLAY_DECIM, disp_taps)
        s2v = blocks.stream_to_vector(gr.sizeof_gr_complex, self.fft_size)
        fftb = fft.fft_vcc(self.fft_size, True, window.blackmanharris(self.fft_size), True)
        mag = blocks.complex_to_mag_squared(self.fft_size)
        log = blocks.nlog10_ff(10.0, self.fft_size, -20.0 * np.log10(self.fft_size))
        self.spec_sink = SpectrumSink(self.pub, "spectrum/0", self.fft_size,
                                      self._spectrum_header, lambda: self._spectrum_on)
        self.connect(self._demod_src, disp, s2v, fftb, mag, log, self.spec_sink)
        self._mode = mode

    def _teardown(self):
        self.stop(); self.wait()
        self.disconnect_all()
        self._vrx.clear()
        self.combiner = None
        self.src = None
        self._demod_src = None
        self._peak_probes = []
        self._null_chain = None            # blocks gone with disconnect_all
        self._null.active = False          # null is meaningless outside this graph
        gc.collect()                # force gr-sdrplay3 source destructor -> device deinit

    # ---- gain / headers ----------------------------------------------
    def _gr_range(self, name):
        """Valid gain-*reduction* range (positive dB) for `name`.

        gr-sdrplay3 reports/accepts a negative 'gain' (e.g. RF (-61,0),
        IF (-59,-20)); the SDRPlay-native 'gain reduction' is its negation.
        Returns (min_gr, max_gr) as positive dB, band-dependent.
        """
        lo, hi = self.src.get_gain_range(name)      # negative 'gain' convention
        return -hi + 0.0, -lo + 0.0                 # +0.0 normalises -0.0 -> 0.0

    def _apply_gain(self):
        """Apply gains. We store/report positive gain *reduction* (protocol
        convention), but gr-sdrplay3 wants negative 'gain', so we negate at the
        boundary. Passing the server's positive values straight through was the
        bug (gotcha #5): the API rejected them as OutOfRange and gains stuck at
        max -> overload. Values are clamped to the live valid range; RF snaps to
        the nearest discrete LNA step. Read-backs record what actually took."""
        g = self.gain
        self.src.set_gain_mode(g["agc"])
        if g["agc"]:
            self.src.set_agc_setpoint(g["agc_setpoint_dbfs"])
        else:
            if_gr = _clamp(g["if_gr_db"], *self._gr_range("IF"))
            self.src.set_gain(-float(if_gr), "IF")
            g["if_gr_db"] = int(round(-self.src.get_gain("IF")))
        rf_gr = _clamp(g["rf_gr_db"], *self._gr_range("RF"))
        self.src.set_gain(-float(rf_gr), "RF")      # driver snaps to nearest LNA step
        g["rf_gr_db"] = int(round(-self.src.get_gain("RF")))   # actual (snapped) value
        self._lna_state = int(self.src.get_gain("LNAstate"))

    def _peak_dbfs(self):
        """Highest raw-stream peak across tuners, in dBFS (0 = fc32/ADC full scale)."""
        if not self._peak_probes:
            return -120.0
        pk = max(p.level() for p in self._peak_probes)
        return float(20.0 * np.log10(pk + 1e-9))

    def _spectrum_header(self):
        peak = self._peak_dbfs()
        return {"source": "combined" if self._mode == "diversity" else "0",
                "center_hz": int(self.center), "span_hz": int(DISPLAY_RATE),
                "fft_size": self.fft_size, "ref_dbfs": 0,
                "peak_dbfs": round(peak, 1), "overload": peak >= OVERLOAD_DBFS}

    # ---- VRX ----------------------------------------------------------
    def _make_vrx(self, freq_hz, mode="lsb", filter=None, volume=0.5, **_):
        if len(self._vrx) >= MAX_VRX:
            raise ProtoError("busy", "max VRX reached")
        if mode not in DEFAULT_FILTERS:
            raise ProtoError("unsupported", f"mode {mode} not implemented")
        if abs(freq_hz - self.center) > SOURCE_RATE / 2 - 50_000:
            raise ProtoError("out_of_range", "VRX outside capture window")
        low, high = (filter["low_hz"], filter["high_hz"]) if filter else DEFAULT_FILTERS[mode]

        xlate = gr_filter.freq_xlating_fir_filter_ccf(
            VRX_DECIM, self._lp_taps, freq_hz - self.center, SOURCE_RATE)
        sb = gr_filter.fir_filter_ccc(1, firdes.complex_band_pass(1.0, INTER_RATE, low, high, 200))
        c2r = blocks.complex_to_real(1)
        ag = analog.agc2_ff(1e-1, 1e-2, 0.3, 1.0); ag.set_max_gain(1_024)
        rs = gr_filter.rational_resampler_fff(interpolation=AUDIO_RATE // 1_000,
                                              decimation=INTER_RATE // 1_000)
        vol = blocks.multiply_const_ff(volume)
        vid = self._next_id; self._next_id += 1
        asink = AudioSink(self.pub, vid, AUDIO_RATE, lambda: self._audio_on)
        return {"vrx_id": vid, "freq": int(freq_hz), "mode": mode,
                "filter": {"low_hz": low, "high_hz": high}, "volume": volume,
                "chain": [xlate, sb, c2r, ag, rs, vol, asink], "xlate": xlate, "vol": vol}

    def _connect_vrx(self, rec):
        self.connect(self._demod_src, rec["chain"][0])
        for a, b in zip(rec["chain"], rec["chain"][1:]):
            self.connect(a, b)

    def add_vrx(self, **params):
        rec = self._make_vrx(**params)
        self.lock()
        self._connect_vrx(rec)
        self.unlock()
        self._vrx[rec["vrx_id"]] = rec
        return self._vrx_public(rec["vrx_id"])

    def update_vrx(self, vrx_id, **kw):
        r = self._vrx.get(vrx_id)
        if not r:
            raise ProtoError("bad_request", f"no vrx {vrx_id}")
        if "freq_hz" in kw:
            r["freq"] = int(kw["freq_hz"]); r["xlate"].set_center_freq(r["freq"] - self.center)
        if "volume" in kw:
            r["volume"] = kw["volume"]; r["vol"].set_k(kw["volume"])
        return self._vrx_public(vrx_id)

    def remove_vrx(self, vrx_id):
        r = self._vrx.pop(vrx_id, None)
        if not r:
            raise ProtoError("bad_request", f"no vrx {vrx_id}")
        self.lock()
        self.disconnect(self._demod_src, r["chain"][0])
        for a, b in zip(r["chain"], r["chain"][1:]):
            self.disconnect(a, b)
        self.unlock()
        return {}

    def _vrx_public(self, vid):
        r = self._vrx[vid]
        return {"vrx_id": vid, "freq_hz": r["freq"], "mode": r["mode"],
                "filter": r["filter"], "volume": r["volume"]}

    # ---- control operations ------------------------------------------
    def set_center_freq(self, hz):
        self.center = int(hz)
        self.src.set_center_freq(self.center)
        for r in self._vrx.values():
            r["xlate"].set_center_freq(r["freq"] - self.center)
        return {"center_hz": self.center}

    def _gain_public(self):
        return {**self.gain, "lna_state": self._lna_state,
                "rf_gr_db_range": list(self._gr_range("RF")),
                "rf_gr_db_steps": self._rf_steps,
                "if_gr_db_range": list(self._gr_range("IF"))}

    def set_gain(self, **kw):
        self.gain.update({k: kw[k] for k in self.gain if k in kw})
        self.gain["agc"] = bool(self.gain["agc"])   # keep the driver bool clean
        self._apply_gain()
        return self._gain_public()

    # ---- diversity null (targeted interference canceller) -------------
    def _null_taps(self, width_hz):
        cutoff = width_hz / 2.0
        trans = max(width_hz / 4.0, 2_000.0)
        return firdes.low_pass(1.0, SOURCE_RATE, cutoff, trans)

    def _engage_null(self, center_hz, width_hz, track):
        if self._mode != "diversity":
            raise ProtoError("wrong_mode", "null requires diversity tuner mode")
        center_hz = int(center_hz)
        width_hz = int(_clamp(width_hz, *NULL_WIDTH_RANGE))
        if abs(center_hz - self.center) > SOURCE_RATE / 2 - width_hz:
            raise ProtoError("out_of_range", "null target outside capture window")
        offset = center_hz - self.center
        taps = self._null_taps(width_hz)
        if self._null_chain is None:
            # build the band-isolation tap on both branches + estimator
            xl0 = gr_filter.freq_xlating_fir_filter_ccf(NULL_DECIM, taps, offset, SOURCE_RATE)
            xl1 = gr_filter.freq_xlating_fir_filter_ccf(NULL_DECIM, taps, offset, SOURCE_RATE)
            est = NullEstimator(self._null)
            self.lock()
            self.connect((self.src, 0), xl0, (est, 0))
            self.connect((self.src, 1), xl1, (est, 1))
            self.unlock()
            self._null_chain = [xl0, xl1, est]
        else:                                   # retarget an existing null
            xl0, xl1, _ = self._null_chain
            for xl in (xl0, xl1):
                xl.set_taps(taps)
                xl.set_center_freq(offset)
        self._null.manual = False
        self._null.track = bool(track)
        self._null.center_hz = center_hz
        self._null.width_hz = width_hz
        self._null.active = True

    def _set_null_weight(self, amp, phase_deg):
        if not self._null.active:
            raise ProtoError("bad_request", "no active null; target one with center_hz first")
        amp = float(amp if amp is not None else abs(self._null.w))
        ph = float(phase_deg if phase_deg is not None else np.degrees(np.angle(self._null.w)))
        self._null.manual = True
        self._null.w = amp * np.exp(1j * np.radians(ph))

    def _clear_null(self):
        if self._null_chain is not None:
            xl0, xl1, est = self._null_chain
            self.lock()
            self.disconnect((self.src, 0), xl0, (est, 0))
            self.disconnect((self.src, 1), xl1, (est, 1))
            self.unlock()
            self._null_chain = None
        self._null.active = False
        self._null.manual = False
        self._null.w = 0 + 0j

    def _set_track_speed(self, speed):
        speed = str(speed).lower()
        if speed not in NULL_ALPHA:
            raise ProtoError("bad_request", f"track_speed must be one of {list(NULL_ALPHA)}")
        self._null.speed = speed
        self._null.alpha = NULL_ALPHA[speed]

    def null_signal(self, clear=False, center_hz=None, width_hz=None,
                    track=None, amp=None, phase_deg=None, track_speed=None, **_):
        """Engage / retarget / trim / clear the diversity interference null."""
        if clear:
            self._clear_null()
            return self._null_public()
        if center_hz is not None:
            self._engage_null(center_hz, width_hz or NULL_WIDTH_DEFAULT,
                              True if track is None else track)
        if track_speed is not None:
            self._set_track_speed(track_speed)
        if amp is not None or phase_deg is not None:
            self._set_null_weight(amp, phase_deg)         # -> manual mode
        elif track is not None and center_hz is None:     # freeze/thaw only
            if not self._null.active:
                raise ProtoError("bad_request", "no active null")
            self._null.track = bool(track)
            if track:
                self._null.manual = False
        return self._null_public()

    def _null_public(self):
        s = self._null
        return {"active": s.active, "track": s.track, "manual": s.manual,
                "amp": float(abs(s.w)), "phase_deg": float(np.degrees(np.angle(s.w))),
                "null_depth_db": round(s.depth_db, 1), "track_speed": s.speed,
                "center_hz": s.center_hz, "width_hz": s.width_hz}

    def set_spectrum(self, rate_hz=None, **_):
        if rate_hz:
            self.spec_sink.set_rate(rate_hz)
        return {"fft_size": self.fft_size, "rate_hz": rate_hz}

    def set_tuner_mode(self, mode):
        if mode == self._mode:
            return self.status()
        if mode not in TUNER_MODES:
            raise ProtoError("unsupported", f"tuner mode {mode} not implemented")
        saved = [{"freq_hz": r["freq"], "mode": r["mode"], "filter": r["filter"],
                  "volume": r["volume"]} for r in self._vrx.values()]
        if self._reconfigure(mode, saved):
            return self.status()
        # persistent failure (dual-tuner init) -> fall back to single
        self._reconfigure("single", saved)
        raise ProtoError("device_error",
                         f"{mode} init failed (dual-tuner unreliable); reverted to single")

    def _reconfigure(self, mode, saved_vrx):
        """Rebuild the graph in `mode`, re-add VRXs, start, and verify samples
        flow. Retries dual-tuner init. Returns True on success."""
        self._teardown()
        attempts = INIT_RETRIES if mode == "diversity" else 1
        for _ in range(attempts):
            self._build(mode)
            for v in saved_vrx:
                rec = self._make_vrx(**v)
                self._connect_vrx(rec)
                self._vrx[rec["vrx_id"]] = rec
            before = self.spec_sink.work_count
            self.start()
            time.sleep(INIT_VERIFY_S)
            if self.spec_sink.work_count > before:
                return True                  # samples flowing -> init OK
            self._teardown()                 # init failed -> reset and retry
            time.sleep(0.5)
        return False

    def status(self):
        st = {
            "protocol_version": PROTOCOL_VERSION,
            "tuner_mode": self._mode,
            "device": {"name": "RSPduo"},
            "capture": {"center_hz": self.center, "sample_rate_hz": SOURCE_RATE,
                        "bandwidth_hz": 1_536_000},
            "gain": self._gain_public(),
            "vrx": [self._vrx_public(v) for v in self._vrx],
            "streaming": {"audio": self._audio_on, "spectrum": self._spectrum_on},
        }
        if self._mode == "diversity" and self.combiner is not None:
            if self._null.active:
                st["combiner"] = {"type": "null", **self._null_public()}
            else:
                g = self.combiner.g
                st["combiner"] = {"type": "mrc", "auto": True, "amp": float(abs(g)),
                                  "phase_deg": float(np.degrees(np.angle(g)))}
        return st


# --------------------------------------------------------------------------
# Control server (REQ/REP)
# --------------------------------------------------------------------------
def control_loop(sock, srv, stop_evt, shutdown_token=None):
    """Serve the control channel on an already-bound REP socket (bound by main()
    before capture starts, so a port clash is fatal rather than a zombie)."""
    poller = zmq.Poller(); poller.register(sock, zmq.POLLIN)
    stopping = False
    caps = {"tuner_modes": list(TUNER_MODES), "demod_modes": list(DEFAULT_FILTERS),
            "max_vrx": MAX_VRX, "sample_rates_hz": [SOURCE_RATE],
            "audio_rate_hz": AUDIO_RATE, "audio_formats": ["int16"],
            "features": ["multi_vrx", "diversity", "diversity_null"]}

    def handle(cmd, p):
        nonlocal stopping
        if cmd == "shutdown":
            # Only the process that launched us (and holds the token) may stop
            # us. A hand-started server has no token, so cannot be stopped remotely.
            if not shutdown_token:
                raise ProtoError("bad_request", "shutdown disabled: server has no --shutdown-token")
            if not hmac.compare_digest(str(p.get("token", "")), shutdown_token):
                raise ProtoError("bad_request", "bad shutdown token")
            stopping = True          # stop_evt is set only AFTER the reply is sent
            return {"stopping": True}
        if cmd == "hello":
            return {"protocol_version": PROTOCOL_VERSION, "server": "hf-sdr-server/0.1"}
        if cmd == "get_status":
            return srv.status()
        if cmd == "get_capabilities":
            return caps
        if cmd == "set_tuner_mode":
            return srv.set_tuner_mode(p.get("mode", "single"))
        if cmd == "set_center_freq":
            return srv.set_center_freq(p["hz"])
        if cmd == "set_gain":
            return srv.set_gain(**p)
        if cmd == "null_signal":
            return srv.null_signal(**p)
        if cmd == "configure_spectrum":
            return srv.set_spectrum(**p)
        if cmd == "add_vrx":
            return srv.add_vrx(**p)
        if cmd == "update_vrx":
            return srv.update_vrx(**p)
        if cmd == "remove_vrx":
            return srv.remove_vrx(**p)
        if cmd == "list_vrx":
            return {"vrx": [srv._vrx_public(v) for v in srv._vrx]}
        if cmd in ("start", "stop"):
            on = (cmd == "start")
            if p.get("audio", True): srv._audio_on = on
            if p.get("spectrum", True): srv._spectrum_on = on
            return srv.status()["streaming"]
        raise ProtoError("unknown_cmd", f"no such command: {cmd}")

    while not stop_evt.is_set():
        if not poller.poll(200):
            continue
        try:
            req = json.loads(sock.recv())
            rid = req.get("id")
            try:
                result = handle(req.get("cmd"), req.get("params") or {})
                sock.send_string(json.dumps({"id": rid, "ok": True, "result": result}))
                if stopping:
                    stop_evt.set()
            except ProtoError as e:
                sock.send_string(json.dumps({"id": rid, "ok": False,
                                             "error": {"code": e.code, "message": e.message}}))
            except Exception as e:  # noqa: BLE001
                sock.send_string(json.dumps({"id": rid, "ok": False,
                                             "error": {"code": "bad_request", "message": str(e)}}))
        except Exception as e:  # malformed frame
            try:
                sock.send_string(json.dumps({"ok": False, "error": {"code": "bad_request", "message": str(e)}}))
            except zmq.ZMQError:
                pass
    sock.close(linger=1000)          # let the final reply (e.g. shutdown ack) flush


def main():
    p = argparse.ArgumentParser(description="HF SDR headless server")
    p.add_argument("--center", type=float, default=7.15e6)
    p.add_argument("--control-port", type=int, default=5555)
    p.add_argument("--stream-port", type=int, default=5556)
    p.add_argument("--audio-port", type=int, default=5557)
    p.add_argument("--bind", default="*",
                   help="interface to bind all three ports on (default '*' = all "
                        "interfaces; use 127.0.0.1 for local-only)")
    p.add_argument("--shutdown-token", default=os.environ.get("HF_SDR_SHUTDOWN_TOKEN"),
                   help="enable the remote 'shutdown' command, gated by this token "
                        "(or env HF_SDR_SHUTDOWN_TOKEN). Without it, shutdown is refused.")
    args = p.parse_args()

    # Bind every socket BEFORE touching the hardware, so a port clash (e.g. a
    # server already running) is a fast, loud, non-zero exit.
    ctx = zmq.Context.instance()
    ctrl_sock = ctx.socket(zmq.REP)
    bind_or_exit(ctrl_sock, args.bind, args.control_port, "control")
    pub = Publisher(ctx, args.bind, args.stream_port, args.audio_port)
    srv = SdrServer(pub, int(args.center))
    stop_evt = threading.Event()
    ctrl = threading.Thread(target=control_loop,
                            args=(ctrl_sock, srv, stop_evt, args.shutdown_token), daemon=True)
    signal.signal(signal.SIGINT, lambda *_: stop_evt.set())
    srv.start()
    ctrl.start()
    print(f"hf-sdr-server: control :{args.control_port}  stream :{args.stream_port}  "
          f"audio :{args.audio_port}  centre {args.center/1e6:.3f} MHz", flush=True)

    # Block until shutdown. NOT srv.wait() (the flowgraph stops/starts on mode
    # switch) and NOT a bare Event.wait() (on Windows that parks in a C call and
    # swallows Ctrl-C). A short timed loop lets SIGINT be delivered.
    try:
        while not stop_evt.wait(0.25):
            pass
    except KeyboardInterrupt:
        stop_evt.set()
    ctrl.join(2)                     # control thread exits + flushes any shutdown ack
    srv.stop(); srv.wait()           # device deinit path, same for Ctrl-C and 'shutdown'


if __name__ == "__main__":
    main()
