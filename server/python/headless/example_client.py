#!/usr/bin/env python3
"""
Reference/integration client for the headless server (protocol/messages.md).

Not the real client (that's the Avalonia app) — this exercises the contract end
to end and doubles as a smoke test / usage example. Start the server first:

    python server.py --center 7.15e6
    python example_client.py
"""
import json
import time

import numpy as np
import zmq

ctx = zmq.Context.instance()
ctrl = ctx.socket(zmq.REQ)
ctrl.setsockopt(zmq.RCVTIMEO, 2000)
ctrl.connect("tcp://localhost:5555")


def cmd(c, **p):
    ctrl.send_string(json.dumps({"id": 1, "cmd": c, "params": p}))
    return json.loads(ctrl.recv())


def main():
    # wait for the server's control socket
    global ctrl
    h = {}
    for _ in range(30):
        try:
            h = cmd("hello", protocol_version="0.1", client="example_client")
            if h.get("ok"):
                break
        except zmq.ZMQError:
            ctrl.close(); ctrl = ctx.socket(zmq.REQ)
            ctrl.setsockopt(zmq.RCVTIMEO, 2000); ctrl.connect("tcp://localhost:5555")
        time.sleep(0.3)
    print("hello       :", h)
    print("capabilities:", cmd("get_capabilities")["result"])

    cmd("configure_spectrum", rate_hz=10)
    r = cmd("add_vrx", freq_hz=7.150e6, mode="lsb")
    print("add_vrx     :", r)
    vid = r["result"]["vrx_id"]

    spec = ctx.socket(zmq.SUB); spec.connect("tcp://localhost:5556")
    spec.setsockopt_string(zmq.SUBSCRIBE, "spectrum")
    aud = ctx.socket(zmq.SUB); aud.connect("tcp://localhost:5557")
    aud.setsockopt_string(zmq.SUBSCRIBE, f"audio/{vid}")

    poller = zmq.Poller(); poller.register(spec, zmq.POLLIN); poller.register(aud, zmq.POLLIN)
    nspec = naud = 0
    t0 = time.time()
    while time.time() - t0 < 6 and (nspec < 3 or naud < 3):
        socks = dict(poller.poll(500))
        if spec in socks:
            topic, hdr, payload = spec.recv_multipart(); hh = json.loads(hdr)
            arr = np.frombuffer(payload, dtype="<f4")
            if nspec == 0:
                print(f"spectrum    : topic={topic.decode()} bins={len(arr)} "
                      f"center={hh['center_hz']} span={hh['span_hz']} peak={arr.max():.1f} dBFS")
            nspec += 1
        if aud in socks:
            topic, hdr, payload = aud.recv_multipart(); hh = json.loads(hdr)
            samp = np.frombuffer(payload, dtype="<i2")
            if naud == 0:
                print(f"audio       : topic={topic.decode()} samples={hh['samples']} rate={hh['rate_hz']}")
            naud += 1

    print(f"received    : {nspec} spectrum, {naud} audio frames")
    print("remove_vrx  :", cmd("remove_vrx", vrx_id=vid))
    print("RESULT      :", "OK" if nspec >= 3 and naud >= 3 else "INCOMPLETE")


if __name__ == "__main__":
    main()
