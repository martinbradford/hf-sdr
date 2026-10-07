import json, os, sys, time, zmq
HOST = os.environ.get("HF_SDR_HOST", "127.0.0.1")
def req(cmd, **p):
    s = zmq.Context.instance().socket(zmq.REQ); s.connect(f"tcp://{HOST}:5554"); s.RCVTIMEO = 5000; s.LINGER = 0
    s.send_string(json.dumps({"id": 1, "cmd": cmd, "params": p})); r = json.loads(s.recv()); s.close(); return r["result"] if r.get("ok") else r
def ctl(cmd, **p):
    s = zmq.Context.instance().socket(zmq.REQ); s.connect(f"tcp://{HOST}:5555"); s.RCVTIMEO = 70000; s.LINGER = 0
    s.send_string(json.dumps({"id": 1, "cmd": cmd, "params": p})); r = json.loads(s.recv()); s.close(); return r
def wait(state, t=40):
    t0 = time.time()
    while time.time() - t0 < t:
        st = req("status")
        if "state" not in st: print("odd reply:", st, flush=True); time.sleep(0.5); continue
        if st["state"] == state: return st
        if st["state"] == "failed": return st
        time.sleep(0.5)
    return st
mode, n = sys.argv[1], int(sys.argv[2]); fails = 0
for i in range(1, n + 1):
    t0 = time.time(); req("start", center_hz=7.15e6, tuner_mode=mode); st = wait("running")
    ok = st["state"] == "running"
    tm = ctl("get_status")["result"]["tuner_mode"] if ok else "-"
    ok = ok and tm == mode and not st.get("last_error")
    t1 = time.time() - t0
    req("stop"); sp = wait("stopped", 15)
    ok = ok and sp["state"] == "stopped" and not sp.get("last_error")
    fails += not ok
    print(f"{mode} #{i}: {'OK' if ok else 'FAIL'}  up {t1:.1f}s  mode={tm}  stop={sp['state']} {sp.get('last_error') or st.get('last_error') or ''}", flush=True)
    if not ok: print((st.get('log_tail') or [])[-4:])
print(f"{mode}: {n - fails}/{n} OK")
