#!/usr/bin/env python3
"""
Release the RSP: make sure no HF-SDR server is holding it, so SDRConnect (or
anything else) can open it.

    python release_rsp.py            # graceful stop via the supervisor; report anything left over
    python release_rsp.py --force    # ...and kill a leftover server the supervisor doesn't own

1. Asks the supervisor (HF_SDR_HOST, default 127.0.0.1, port 5554) to `stop` the server and waits for
   it to exit. This is the graceful path (the server's own `shutdown` => SDRPlay device deinit).
2. Looks for anything still listening on the server ports (5555-5557). A hand-started server has no
   shutdown token and the supervisor doesn't know it, so it cannot be stopped remotely: this reports
   its PID and, with --force, kills it. A kill skips device deinit and can wedge the RSP; if SDRConnect
   then cannot open it, open + close SDRConnect once, or Restart-Service SDRplayAPIService.

Local only for step 2 (it inspects this PC's ports). Exit status: 0 = receiver released, 1 = not.
"""
import json
import os
import re
import subprocess
import sys
import time

import zmq

HOST = os.environ.get("HF_SDR_HOST", "127.0.0.1")
SUP_PORT = 5554
SERVER_PORTS = (5555, 5556, 5557)
LOCAL = HOST in ("127.0.0.1", "localhost", "::1")


def sup_call(cmd, timeout_ms=3000):
    s = zmq.Context.instance().socket(zmq.REQ)
    s.setsockopt(zmq.LINGER, 0)
    s.RCVTIMEO = timeout_ms
    s.connect(f"tcp://{HOST}:{SUP_PORT}")
    try:
        s.send_string(json.dumps({"id": 1, "cmd": cmd, "params": {}}))
        r = json.loads(s.recv())
        return r["result"] if r.get("ok") else None
    except zmq.Again:
        return None
    finally:
        s.close()


def release_via_supervisor():
    st = sup_call("status")
    if st is None:
        print(f"supervisor: not reachable at {HOST}:{SUP_PORT} (service stopped?)")
        return
    state = st["state"]
    print(f"supervisor: server is '{state}'" + (f" (pid {st['pid']})" if "pid" in st else ""))
    if state not in ("running", "starting", "stopping"):
        return
    if state != "stopping":
        sup_call("stop")
        print("supervisor: stop requested, waiting for the server to exit...")
    deadline = time.time() + 20          # supervisor allows 5 s graceful, then kills
    while time.time() < deadline:
        st = sup_call("status")
        if st and st["state"] in ("stopped", "failed"):
            print(f"supervisor: server '{st['state']}'" + (f" — {st['last_error']}" if st.get("last_error") else ""))
            return
        time.sleep(0.5)
    print("supervisor: server still not stopped after 20 s")


def listeners():
    """{port: pid} for the server ports, from netstat (no extra dependencies, works unelevated)."""
    out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True).stdout
    found = {}
    for line in out.splitlines():
        m = re.match(r"\s*TCP\s+\S+:(\d+)\s+\S+\s+LISTENING\s+(\d+)", line)
        if m and int(m.group(1)) in SERVER_PORTS:
            found[int(m.group(1))] = int(m.group(2))
    return found


def process_name(pid):
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True).stdout.strip()
    return out.split('","')[0].strip('"') if out and not out.startswith("INFO") else "?"


def main():
    force = "--force" in sys.argv[1:]
    release_via_supervisor()
    if not LOCAL:
        print(f"(host is {HOST}: leftover-server check skipped, run this on that PC)")
        return 0

    time.sleep(0.5)
    left = listeners()
    if not left:
        print("ports 5555-5557: nothing listening — receiver released")
        return 0

    pids = sorted(set(left.values()))
    for pid in pids:
        ports = ", ".join(str(p) for p, q in sorted(left.items()) if q == pid)
        print(f"still listening: pid {pid} ({process_name(pid)}) on port(s) {ports}")
    if not force:
        print("This server is not managed by the supervisor (probably started by hand). "
              "Close its console window / Ctrl-C it, or re-run with --force to kill it "
              "(skips device deinit; the RSP may need an SDRConnect open+close to recover).")
        return 1
    for pid in pids:
        r = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True)
        print(f"taskkill pid {pid}: {(r.stdout or r.stderr).strip()}")
    time.sleep(1.0)
    left = listeners()
    print("receiver released" if not left else f"still listening after kill: {left} (needs an elevated prompt?)")
    return 0 if not left else 1


if __name__ == "__main__":
    sys.exit(main())
