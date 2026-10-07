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
