#!/usr/bin/env python3
"""Unit test for the server lifecycle plumbing (protocol/server_lifecycle.md §4.1, §4.2).

No hardware needed — and no GNU Radio either: if `gnuradio` is not importable
the test installs inert stubs so server.py can be imported and main() exercised
with a fake SdrServer. Checks:

  (1) a clashing control/stream/audio port is fatal (exit 2) BEFORE the
      hardware-owning SdrServer is constructed (no zombie server),
  (2) `shutdown` is refused with no token configured, and with a wrong token,
  (3) `shutdown` with the right token is acknowledged, THEN the server stops
      (srv.stop()/srv.wait() run — the device-deinit path),
  (4) ordinary commands (`hello`) are unaffected,
  (5) the Publisher counts frames it drops when its send queue is full.

Run:  python server\\python\\headless\\test_lifecycle.py   (needs pyzmq + numpy)
"""
import json
import socket
import sys
import threading
import time
from unittest import mock

import zmq


from gr_stubs import install_gnuradio_stubs  # noqa: E402


install_gnuradio_stubs()
import server  # noqa: E402


def free_ports(n):
    socks = [socket.socket() for _ in range(n)]
    for s in socks:
        s.bind(("127.0.0.1", 0))
    ports = [s.getsockname()[1] for s in socks]
    for s in socks:
        s.close()
    return ports


class FakeSrv:
    """Stands in for SdrServer: records that stop()/wait() ran."""
    instances = []

    def __init__(self, pub, center):
        FakeSrv.instances.append(self)
        self.stopped = self.waited = False
        self._vrx = []

    def start(self): pass
    def stop(self): self.stopped = True
    def wait(self): self.waited = True


def call(port, cmd, params=None, timeout_ms=3000):
    s = zmq.Context.instance().socket(zmq.REQ)
    s.setsockopt(zmq.LINGER, 0)
    s.setsockopt(zmq.RCVTIMEO, timeout_ms)
    s.connect(f"tcp://127.0.0.1:{port}")
    try:
        s.send_string(json.dumps({"id": 1, "cmd": cmd, "params": params or {}}))
        return json.loads(s.recv())
    finally:
        s.close()


def run_main(argv, driver=None):
    """Run server.main() on this thread (it installs a SIGINT handler); `driver`
    runs on a helper thread to talk to it. Returns (exit_code_or_None)."""
    result = {}
    t = None
    if driver:
        t = threading.Thread(target=lambda: result.update(driver()), daemon=True)
        t.start()
    with mock.patch.object(sys, "argv", ["server.py"] + argv), \
            mock.patch.object(server, "SdrServer", FakeSrv):
        try:
            server.main()
            code = None
        except SystemExit as e:
            code = e.code
    if t:
        t.join(5)
    return code, result


def test_port_clash():
    print("-- port clash is fatal before hardware init --")
    for clash in ("control", "stream", "audio"):
        cp, sp, ap = free_ports(3)
        hog = zmq.Context.instance().socket(zmq.REP)
        hog.bind(f"tcp://127.0.0.1:{ {'control': cp, 'stream': sp, 'audio': ap}[clash] }")
        FakeSrv.instances.clear()
        code, _ = run_main(["--bind", "127.0.0.1", "--control-port", str(cp),
                            "--stream-port", str(sp), "--audio-port", str(ap)])
        hog.close(linger=0)
        assert code == 2, f"{clash}: expected exit 2, got {code!r}"
        assert not FakeSrv.instances, f"{clash}: SdrServer constructed despite bind failure"
        print(f"  {clash} clash -> exit 2, no SdrServer constructed  OK")


def test_shutdown_refused_without_token():
    print("-- shutdown refused when no token configured --")
    cp, sp, ap = free_ports(3)
    FakeSrv.instances.clear()

    def driver():
        time.sleep(0.5)
        r_hello = call(cp, "hello")
        r_sd = call(cp, "shutdown", {"token": "anything"})
        # server must still be up; stop it the only other way main() allows
        server_stop_evt_hack()
        return {"hello": r_hello, "sd": r_sd}

    code, res = _run_with_manual_stop(cp, sp, ap, [], driver)
    assert res["hello"]["ok"], res
    assert not res["sd"]["ok"] and res["sd"]["error"]["code"] == "bad_request", res["sd"]
    print("  no token configured -> bad_request, server kept running  OK")


# main() blocks on a private Event; to end a test whose shutdown is *refused* we
# raise KeyboardInterrupt in the main thread, exactly as Ctrl-C would.
def server_stop_evt_hack():
    import _thread
    _thread.interrupt_main()


def _run_with_manual_stop(cp, sp, ap, extra, driver):
    return run_main(["--bind", "127.0.0.1", "--control-port", str(cp),
                     "--stream-port", str(sp), "--audio-port", str(ap)] + extra, driver)


def test_shutdown_token():
    print("-- shutdown: wrong token refused, right token acked then stops --")
    cp, sp, ap = free_ports(3)
    FakeSrv.instances.clear()

    def driver():
        time.sleep(0.5)
        bad = call(cp, "shutdown", {"token": "nope"})
        missing = call(cp, "shutdown", {})
        still_up = call(cp, "hello")
        good = call(cp, "shutdown", {"token": "s3cret"})
        return {"bad": bad, "missing": missing, "still_up": still_up, "good": good}

    code, res = _run_with_manual_stop(cp, sp, ap, ["--shutdown-token", "s3cret"], driver)
    assert code is None, f"main should return normally, got exit {code!r}"
    for k in ("bad", "missing"):
        assert not res[k]["ok"] and res[k]["error"]["code"] == "bad_request", (k, res[k])
    assert res["still_up"]["ok"], "server died on a refused shutdown"
    assert res["good"] == {"id": 1, "ok": True, "result": {"stopping": True}}, res["good"]
    srv = FakeSrv.instances[0]
    assert srv.stopped and srv.waited, "srv.stop()/wait() not run after shutdown"
    print("  bad/missing token -> bad_request; right token -> ack, then stop()+wait()  OK")


def test_publisher_drop_counter():
    print("-- Publisher counts frames dropped when its send queue is full --")
    import queue
    cp, sp, ap = free_ports(3)
    pub = server.Publisher(zmq.Context.instance(), "127.0.0.1", sp, ap)
    pub._run = False                    # stop the drain thread so the queue can fill
    time.sleep(0.5)
    pub._q = queue.Queue(maxsize=2)
    for _ in range(5):
        pub.send("audio", "audio/1", {}, b"x")
    pub.send("stream", "spectrum/0", {}, b"x")      # queue already full -> counted as stream
    assert pub.dropped == {"stream": 1, "audio": 3}, pub.dropped
    print("  5 audio sends into a 2-deep queue -> 3 dropped; spectrum counted separately  OK")


if __name__ == "__main__":
    test_publisher_drop_counter()
    test_port_clash()
    test_shutdown_refused_without_token()
    test_shutdown_token()
    print("ALL PASS")
