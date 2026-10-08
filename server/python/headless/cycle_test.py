#!/usr/bin/env python3
"""
Single <-> diversity switching reliability test, over the control channel.

Start the server first (it owns the hardware), then run this against it:

    python server.py --center 7.15e6
    python cycle_test.py                  # 10 round trips against localhost
    python cycle_test.py --cycles 25 --host shack-pc

Each step asks for a tuner mode, then checks (1) the reply is ok, (2) the
reported tuner_mode is the one requested (a failed dual-tuner init makes the
server fall back to single and reply device_error), and (3) spectrum frames are
actually flowing on the stream socket. Prints a per-step line and a summary;
exit code 0 only if every step passed.

This is the portability / regression check for gotcha #3 in CLAUDE.md (clean
dual-tuner init). Run it on any new platform or gr-sdrplay3 / SDRplay API build
before trusting diversity there.
"""
import argparse
import json
import sys
import time

import zmq

ctx = zmq.Context.instance()


def call(host, port, cmd, params=None, timeout_ms=30000):
    """One request on a throwaway REQ socket (a timed-out REQ socket is wedged)."""
    s = ctx.socket(zmq.REQ)
    s.setsockopt(zmq.LINGER, 0)
    s.setsockopt(zmq.RCVTIMEO, timeout_ms)
    s.connect(f"tcp://{host}:{port}")
    try:
        s.send_string(json.dumps({"id": 1, "cmd": cmd, "params": params or {}}))
        return json.loads(s.recv())
    except zmq.Again:
        return {"ok": False, "error": {"code": "timeout",
                                       "message": f"no reply to {cmd} in {timeout_ms} ms"}}
    finally:
        s.close()


def count_spectrum(sub, window_s, want):
    """Count spectrum frames for up to window_s; stop early once `want` seen."""
    n, end = 0, time.time() + window_s
    while time.time() < end and n < want:
        if sub.poll(200):
            sub.recv_multipart()
            n += 1
    return n


def step(args, sub, mode):
    t0 = time.time()
    r = call(args.host, args.control_port, "set_tuner_mode", {"mode": mode})
    dt = time.time() - t0
    if not r.get("ok"):
        e = r.get("error", {})
        return False, dt, f"{e.get('code')}: {e.get('message')}"
    # drop frames queued from before the switch, then require fresh ones
    while sub.poll(0):
        sub.recv_multipart()
    got = count_spectrum(sub, args.flow_window, args.min_frames)
    st = call(args.host, args.control_port, "get_status")
    actual = st.get("result", {}).get("tuner_mode")
    if actual != mode:
        return False, dt, f"status reports tuner_mode={actual!r}, wanted {mode!r}"
    if got < args.min_frames:
        return False, dt, f"only {got} spectrum frame(s) in {args.flow_window:.0f} s"
    return True, dt, f"{got} frames"


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--host", default="localhost")
    p.add_argument("--control-port", type=int, default=5555)
    p.add_argument("--stream-port", type=int, default=5556)
    p.add_argument("--cycles", type=int, default=10,
                   help="round trips; each is diversity then single (default 10)")
    p.add_argument("--flow-window", type=float, default=6.0,
                   help="seconds to wait for spectrum frames after each switch")
    p.add_argument("--min-frames", type=int, default=3)
    args = p.parse_args()

    h = call(args.host, args.control_port, "hello", {"protocol_version": "0.1",
                                                    "client": "cycle_test"}, 3000)
    if not h.get("ok"):
        sys.exit(f"cannot reach server at {args.host}:{args.control_port}: {h.get('error')}")
    print(f"server: {h['result'].get('server')}  protocol {h['result'].get('protocol_version')}")

    sub = ctx.socket(zmq.SUB)
    sub.setsockopt(zmq.LINGER, 0)
    sub.connect(f"tcp://{args.host}:{args.stream_port}")
    sub.setsockopt_string(zmq.SUBSCRIBE, "spectrum")

    # Start from a known state so the first diversity switch is a real transition.
    ok, dt, msg = step(args, sub, "single")
    print(f"  setup  -> single      {'ok  ' if ok else 'FAIL'} {dt:5.1f}s  {msg}")
    if not ok:
        sys.exit("cannot establish a working single-tuner baseline; aborting")

    results = []
    for i in range(1, args.cycles + 1):
        for mode in ("diversity", "single"):
            ok, dt, msg = step(args, sub, mode)
            results.append((i, mode, ok, dt, msg))
            print(f"  {i:>3}/{args.cycles} -> {mode:<9}  {'ok  ' if ok else 'FAIL'} {dt:5.1f}s  {msg}")

    bad = [r for r in results if not r[2]]
    div = [r for r in results if r[1] == "diversity"]
    div_ok = sum(1 for r in div if r[2])
    print()
    print(f"diversity switches : {div_ok}/{len(div)} ok")
    print(f"single switches    : {sum(1 for r in results if r[1] == 'single' and r[2])}"
          f"/{sum(1 for r in results if r[1] == 'single')} ok")
    if results:
        print(f"switch time        : median {sorted(r[3] for r in results)[len(results)//2]:.1f} s,"
              f" max {max(r[3] for r in results):.1f} s")
    print("RESULT             :", "PASS" if not bad else f"FAIL ({len(bad)} failed step(s))")
    sys.exit(0 if not bad else 1)


if __name__ == "__main__":
    main()
