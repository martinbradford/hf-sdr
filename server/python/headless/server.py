#!/usr/bin/env python3
"""
HF SDR headless server (Stage 4) — implements protocol/messages.md v0.1.

Single-tuner capture (the reliable path) with:
  - a spectrum publisher (stream socket),
  - dynamically add/removable VRX demod chains (audio socket), via lock()/unlock(),
  - a JSON REQ/REP control server.

Diversity / independent tuner modes are defined in the contract but not yet
implemented here (dual-tuner init is unreliable — see docs/SETUP_NOTES.md);
those commands return `unsupported` for now.

    python server.py --center 7.15e6
"""

import argparse
import json
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

DEFAULT_FILTERS = {           # audio passband edges (Hz) per mode
    "lsb": (-2400, -300),
    "usb": (300, 2400),
    "cw":  (400, 900),
}


# --------------------------------------------------------------------------
# Publisher: one thread owns the PUB sockets (zmq sockets are not thread-safe,
# and many GNU Radio work() threads feed them). Sinks enqueue; this drains.
# --------------------------------------------------------------------------
class Publisher:
    def __init__(self, ctx, stream_port, audio_port):
        self._stream = ctx.socket(zmq.PUB); self._stream.bind(f"tcp://*:{stream_port}")
        self._audio = ctx.socket(zmq.PUB); self._audio.bind(f"tcp://*:{audio_port}")
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
# Custom sinks
# --------------------------------------------------------------------------
class SpectrumSink(gr.sync_block):
    """Vector float32[fft_size] -> stream socket, time-throttled to rate_hz."""
    def __init__(self, pub, topic, fft_size, header_fn, enabled_fn, rate_hz=15.0):
        gr.sync_block.__init__(self, "spectrum_sink", [(np.float32, fft_size)], [])
        self._pub, self._topic, self._hdr, self._en = pub, topic, header_fn, enabled_fn
        self._period = 1.0 / rate_hz
        self._last = 0.0
        self._seq = 0

    def set_rate(self, rate_hz):
        self._period = 1.0 / max(0.1, rate_hz)

    def work(self, input_items, output_items):
        inp = input_items[0]
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
        self.center = center
        self.fft_size = fft_size
        self.gain = {"agc": True, "if_gr_db": 40, "rf_gr_db": 0, "agc_setpoint_dbfs": -30}
        self._vrx = {}
        self._next_id = 1
        self._audio_on = True
        self._spectrum_on = True
        self._lp_taps = firdes.low_pass(1.0, SOURCE_RATE, 15_000, 5_000)

        # ---- source (single tuner) ----
        self.src = sdrplay3.rspduo(
            "", rspduo_mode="Single Tuner", antenna="Tuner 1 50 ohm",
            stream_args=sdrplay3.stream_args(output_type="fc32", channels_size=1))
        self.src.set_sample_rate(SOURCE_RATE)
        self.src.set_center_freq(center)
        self.src.set_bandwidth(1_536_000)
        self._apply_gain()
        self.src.set_dc_offset_mode(True)
        self.src.set_iq_balance_mode(True)

        # ---- spectrum chain ----
        disp_taps = firdes.low_pass(1.0, SOURCE_RATE, DISPLAY_RATE * 0.45, DISPLAY_RATE * 0.10)
        disp = gr_filter.fir_filter_ccf(DISPLAY_DECIM, disp_taps)
        s2v = blocks.stream_to_vector(gr.sizeof_gr_complex, fft_size)
        fftb = fft.fft_vcc(fft_size, True, window.blackmanharris(fft_size), True)
        mag = blocks.complex_to_mag_squared(fft_size)
        # 10*log10(mag^2) with an FFT-gain normalisation -> approx dBFS
        log = blocks.nlog10_ff(10.0, fft_size, -20.0 * np.log10(fft_size))
        self.spec_sink = SpectrumSink(pub, "spectrum/0", fft_size,
                                      self._spectrum_header, lambda: self._spectrum_on)
        self.connect(self.src, disp, s2v, fftb, mag, log, self.spec_sink)

    # ---- helpers ----
    def _apply_gain(self):
        g = self.gain
        self.src.set_gain_mode(g["agc"])
        if g["agc"]:
            self.src.set_agc_setpoint(g["agc_setpoint_dbfs"])
        else:
            self.src.set_gain(g["if_gr_db"], "IF")
        self.src.set_gain(g["rf_gr_db"], "RF")

    def _spectrum_header(self):
        return {"source": "0", "center_hz": int(self.center), "span_hz": int(DISPLAY_RATE),
                "fft_size": self.fft_size, "ref_dbfs": 0}

    # ---- control operations (called from control thread) ----
    def set_center_freq(self, hz):
        self.center = int(hz)
        self.src.set_center_freq(self.center)
        for r in self._vrx.values():
            r["xlate"].set_center_freq(r["freq"] - self.center)
        return {"center_hz": self.center}

    def set_gain(self, **kw):
        self.gain.update({k: kw[k] for k in self.gain if k in kw})
        self._apply_gain()
        return dict(self.gain)

    def set_spectrum(self, rate_hz=None, **_):
        if rate_hz:
            self.spec_sink.set_rate(rate_hz)
        return {"fft_size": self.fft_size, "rate_hz": rate_hz}

    def add_vrx(self, freq_hz, mode="lsb", filter=None, volume=0.5, **_):
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
        chain = [xlate, sb, c2r, ag, rs, vol, asink]

        self.lock()
        self.connect(self.src, xlate, sb, c2r, ag, rs, vol, asink)
        self.unlock()
        self._vrx[vid] = {"vrx_id": vid, "freq": int(freq_hz), "mode": mode,
                          "filter": {"low_hz": low, "high_hz": high}, "volume": volume,
                          "chain": chain, "xlate": xlate, "vol": vol}
        return self._vrx_public(vid)

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
        self.disconnect(self.src, r["chain"][0])
        for a, b in zip(r["chain"], r["chain"][1:]):
            self.disconnect(a, b)
        self.unlock()
        return {}

    def _vrx_public(self, vid):
        r = self._vrx[vid]
        return {"vrx_id": vid, "freq_hz": r["freq"], "mode": r["mode"],
                "filter": r["filter"], "volume": r["volume"]}

    def status(self):
        return {
            "protocol_version": PROTOCOL_VERSION,
            "tuner_mode": "single",
            "device": {"name": "RSPduo"},
            "capture": {"center_hz": self.center, "sample_rate_hz": SOURCE_RATE,
                        "bandwidth_hz": 1_536_000},
            "gain": dict(self.gain),
            "vrx": [self._vrx_public(v) for v in self._vrx],
            "streaming": {"audio": self._audio_on, "spectrum": self._spectrum_on},
        }


class ProtoError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


# --------------------------------------------------------------------------
# Control server (REQ/REP)
# --------------------------------------------------------------------------
def control_loop(ctx, port, srv, stop_evt):
    sock = ctx.socket(zmq.REP)
    sock.bind(f"tcp://*:{port}")
    poller = zmq.Poller(); poller.register(sock, zmq.POLLIN)
    caps = {"tuner_modes": ["single"], "demod_modes": list(DEFAULT_FILTERS),
            "max_vrx": MAX_VRX, "sample_rates_hz": [SOURCE_RATE],
            "audio_rate_hz": AUDIO_RATE, "audio_formats": ["int16"],
            "features": ["multi_vrx"]}

    def handle(cmd, p):
        if cmd == "hello":
            return {"protocol_version": PROTOCOL_VERSION, "server": "hf-sdr-server/0.1"}
        if cmd == "get_status":
            return srv.status()
        if cmd == "get_capabilities":
            return caps
        if cmd == "set_tuner_mode":
            if p.get("mode") != "single":
                raise ProtoError("unsupported", "only single-tuner implemented")
            return srv.status()
        if cmd == "set_center_freq":
            return srv.set_center_freq(p["hz"])
        if cmd == "set_gain":
            return srv.set_gain(**p)
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


def main():
    p = argparse.ArgumentParser(description="HF SDR headless server")
    p.add_argument("--center", type=float, default=7.15e6)
    p.add_argument("--control-port", type=int, default=5555)
    p.add_argument("--stream-port", type=int, default=5556)
    p.add_argument("--audio-port", type=int, default=5557)
    args = p.parse_args()

    ctx = zmq.Context.instance()
    pub = Publisher(ctx, args.stream_port, args.audio_port)
    srv = SdrServer(pub, int(args.center))
    stop_evt = threading.Event()
    ctrl = threading.Thread(target=control_loop,
                            args=(ctx, args.control_port, srv, stop_evt), daemon=True)
    srv.start()
    ctrl.start()
    print(f"hf-sdr-server: control :{args.control_port}  stream :{args.stream_port}  "
          f"audio :{args.audio_port}  centre {args.center/1e6:.3f} MHz", flush=True)

    def stop(*_):
        stop_evt.set(); srv.stop(); srv.wait(); sys.exit(0)
    signal.signal(signal.SIGINT, stop)
    srv.wait()


if __name__ == "__main__":
    main()
