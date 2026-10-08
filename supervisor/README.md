# HF SDR supervisor

Tiny always-on Windows service (.NET Worker Service) that starts/stops/reports on the Python
server so a client on another PC can launch it. The server itself is deliberately *not* a
service (it would hold the RSP). Design: [`protocol/server_lifecycle.md`](../protocol/server_lifecycle.md) §12.

Protocol: REQ/REP JSON on port **5554** — `status`, `start {center_hz?, tuner_mode?}`, `stop`.
Config (paths, ports, timeouts): `HfSdr.Supervisor/supervisor.json`, read from next to the exe.

## Run as a console app (testing)

```powershell
dotnet run --project supervisor\HfSdr.Supervisor
C:\Users\MABY\radioconda\python.exe server\python\headless\ctl.py   # ctl.py targets 5555; for 5554 use any REQ client
```

## Install as a service (elevated PowerShell)

```powershell
dotnet publish supervisor\HfSdr.Supervisor -c Release -o C:\ProgramData\hf-sdr\supervisor
New-Service HfSdrSupervisor -BinaryPathName C:\ProgramData\hf-sdr\supervisor\HfSdr.Supervisor.exe `
    -DisplayName "HF SDR supervisor" -StartupType AutomaticDelayedStart
Start-Service HfSdrSupervisor
```

Runs as LocalSystem first (§12.6) — check it can read radioconda and import `gnuradio.sdrplay3`;
otherwise set the service logon to the owner's account. Firewall: scope 5554–5557 to the home
and VPN subnets only.

## Test tools (`supervisor/tools/`)

Run with a Python that has `pyzmq`. On the shack PC that is the radioconda interpreter; a plain `python`
on the PATH usually lacks it (`ModuleNotFoundError: No module named 'zmq'`). Point at a remote shack PC
with `HF_SDR_HOST`.

```powershell
$py = "C:\Users\MABY\radioconda\python.exe"
$env:HF_SDR_HOST = "new-shack-pc.local"        # omit when running on the shack PC itself
& $py supervisor\tools\sup_ctl.py status
& $py supervisor\tools\sup_ctl.py start '{"center_hz":7.15e6,"tuner_mode":"diversity"}'
& $py supervisor\tools\sup_ctl.py stop
& $py supervisor\tools\cycle.py diversity 10     # start/verify mode/stop, N times; prints OK/FAIL per cycle
```

**After a `git pull`, restart the server** (`stop` then `start`): a running server keeps executing the
code it started with. `hello` reports the `git` commit it started from and its feature flags, and the
client warns on connect if the server is out of date.
