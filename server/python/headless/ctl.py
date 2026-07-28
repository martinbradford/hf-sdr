#!/usr/bin/env python3
"""
Tiny control-channel CLI for the headless server. Sends one command and prints
the reply. Values are parsed as JSON where possible (so numbers/bools work).

    python ctl.py get_status
    python ctl.py set_tuner_mode mode=diversity
    python ctl.py set_tuner_mode mode=single
    python ctl.py add_vrx freq_hz=7.15e6 mode=lsb
"""
import json
import sys

import zmq

HOST = "localhost"
PORT = 5555


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd = sys.argv[1]
    params = {}
    for kv in sys.argv[2:]:
        k, v = kv.split("=", 1)
        try:
            v = json.loads(v)          # numbers, bools, null; else keep string
        except ValueError:
            pass
        params[k] = v

    ctx = zmq.Context.instance()
    sock = ctx.socket(zmq.REQ)
    sock.setsockopt(zmq.RCVTIMEO, 15000)   # mode switch w/ retries can take ~8-10 s
    sock.connect(f"tcp://{HOST}:{PORT}")
    sock.send_string(json.dumps({"id": 1, "cmd": cmd, "params": params}))
    try:
        print(json.dumps(json.loads(sock.recv()), indent=2))
    except zmq.Again:
        print("(no reply within 15 s — server busy or not running)")


if __name__ == "__main__":
    main()
