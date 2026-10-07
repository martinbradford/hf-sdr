import json, os, sys, zmq
HOST = os.environ.get("HF_SDR_HOST", "127.0.0.1")
s = zmq.Context.instance().socket(zmq.REQ); s.connect(f"tcp://{HOST}:5554"); s.RCVTIMEO = 5000
cmd = sys.argv[1]; params = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
s.send_string(json.dumps({"id": 1, "cmd": cmd, "params": params})); r = json.loads(s.recv())
print(json.dumps(r["result"] if r.get("ok") else r))
