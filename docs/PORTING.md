# Porting notes: Linux and macOS

**Status:** assessment only. Nothing here has been run on Linux or macOS. Everything so far
(including every hardware result in this repo) is from Windows 11. Statements about the SDRplay
API, `gr-sdrplay3` and distro packaging on other platforms are **expectations to verify**, not
facts. Windows-specific code locations were found by searching the tree on 2026-10-08.

## 1. Summary

The architecture is already portable: the DSP server is Python + GNU Radio + ZeroMQ, the client is
Avalonia/.NET, and the two meet only over sockets. The Windows-specific code is confined to a few
places. **The real risk is not the code, it is whether dual-tuner (diversity) init/deinit behaves
the same on another OS** (section 4). Test that before porting anything.

| Platform | Code effort (estimate) | Biggest risk |
|----------|-----------------------|--------------|
| Linux | 2-4 days | Dual-tuner init/deinit behaviour (diversity) |
| macOS | Linux effort, plus launchd and audio/virtual-device differences | As Linux, plus availability and quality of `gr-sdrplay3` and the SDRplay API for the Mac's CPU architecture |

## 2. Step zero: prove the hardware path (server only, no client)

Do this before any porting work. It needs no C# at all.

1. Install, for the platform: the SDRplay API (its service daemon must be running), GNU Radio 3.10.x,
   and `gr-sdrplay3` built against that API version. (Windows used a prebuilt package from
   fventuri's GitHub releases; see `docs/SETUP_NOTES.md`. Do **not** use GNU Radio 4.x.)
2. Start the server: `python server/python/headless/server.py --center 7.15e6`
   (add `--bind 127.0.0.1` for local-only).
3. Smoke test: `python server/python/headless/example_client.py` (expects spectrum + audio frames).
4. **Diversity switching test:** `python server/python/headless/cycle_test.py --cycles 10`.
   This flips single <-> diversity repeatedly over the control channel and checks, per step, that the
   reply is ok, the reported mode is the one requested, and spectrum frames are flowing. A failed
   dual-tuner init shows up as `device_error` (the server falls back to single). Exit code 0 only if
   every step passed.
5. **Shutdown/restart cycle:** start with `--shutdown-token test`, then `ctl.py shutdown token=test`,
   restart immediately, repeat about 10 times in each mode (design: `protocol/server_lifecycle.md`
   section 12.9). Use a non-numeric, non-bool token (`ctl.py` JSON-parses values).

On the Windows shack PC these cycles passed 10/10 in both modes. Compare against that.

## 3. Work list (code)

Windows-specific spots, and what each needs:

| Where | Problem | Fix |
|-------|---------|-----|
| `client/HfSdr.App/MainWindow.axaml.cs` (`WasapiOut`, NAudio `CoreAudioApi`), `HfSdr.App.csproj` (NAudio) | Audio output is Windows-only. **Largest client job.** | Put audio output behind an interface (48 kHz, 16-bit, mono buffer in; device list of names; select device). Keep NAudio/WASAPI as the Windows backend; add one cross-platform backend (e.g. OpenAL, PortAudio or miniaudio bindings). The device-selector UI should need only the interface. |
| `supervisor/.../JobObject.cs`, `ServerHost.cs` | Job Object (`kernel32`) reaps the Python child if the supervisor dies. `ServerHost` constructs it unconditionally, so the supervisor will not run off Windows. | Make child-reaping an interface. Linux: `PR_SET_PDEATHSIG` on the child, or run under a systemd unit with `KillMode=control-group`. macOS has no equivalent of `PDEATHSIG`; use a process group plus the child watching its parent (or launchd's own process management). |
| `supervisor/.../Program.cs` (`AddWindowsService`) | Windows service host. | Linux: add `UseSystemd()` (supported by the .NET hosting packages) and a unit file. macOS: no built-in launchd host; run as a plain console host under a LaunchDaemon plist. |
| `supervisor/.../SupervisorOptions.cs`, `supervisor.json` | Hardcoded `C:/Users/MABY/radioconda/python.exe`. | Per-platform defaults / require configuration; document it. |
| `RunServer.ps1` | PowerShell launcher. | Add a `.sh` equivalent. |
| Docs/setup | Windows-specific install notes (Norton TLS, `.conda` from fventuri's releases). | Add per-platform setup notes once someone has done it. |

Already portable, no change needed: the Python server, the wire protocol, the Avalonia UI, and the
client settings path (`ClientSettings.cs` uses `Environment.SpecialFolder.ApplicationData`, which
resolves to `~/.config` on Linux and `~/Library/Application Support` on macOS).

## 4. Risks (need hardware to answer)

1. **Dual-tuner init/deinit (diversity).** The `gc.collect()` trick in `set_tuner_mode` (CLAUDE.md
   gotcha #3) works around the API wedging after an unclean deinit. The behaviour of the SDRplay API
   service on another OS or API build may differ, for better or worse. The server already degrades
   gracefully (3 retries, then fall back to single and report `device_error`), so the worst case is
   unreliable diversity, not a dead receiver. `cycle_test.py` (section 2) is the check. Anyone relying on
   diversity should treat it as **unproven** until it passes there.
2. **SDRplay API availability.** Confirm a supported API installer exists for the specific OS version
   and CPU architecture, and which `gr-sdrplay3` version matches it. Check the RSPduo explicitly,
   not just single-tuner models.
3. **Device access.** Linux: udev rules / user group for USB access. macOS: no udev, but check the
   OS's privacy and network prompts for the service and the ports.
4. **Audio latency/jitter** with the chosen backend (untested).

## 5. Platform notes

### Linux
- GNU Radio 3.10 is a first-class platform. Get it from the distro or from radioconda's Linux build.
- `gr-sdrplay3`: build from source against the SDRplay Linux API unless a package exists.
- Routing the receiver's audio into another program (WSJT-X etc.) is usually *easier* than on Windows
  (PulseAudio/PipeWire loopback sinks, no virtual-cable install).
- systemd gives restart and logging; the supervisor needs no password-store workaround as on Windows
  service accounts.

### macOS
- Python/GNU Radio: radioconda publishes macOS builds for both Intel and Apple Silicon; Homebrew and
  MacPorts also carry GNU Radio. Prefer the same conda route used on Windows for matching versions.
- Verify before committing time: that `gr-sdrplay3` builds against the macOS SDRplay API, and that the
  API supports the RSPduo on the user's Mac architecture.
- Audio into other apps needs a virtual audio device (BlackHole or similar); the device selector lists
  whatever CoreAudio exposes.
- launchd replaces systemd; there is no `PDEATHSIG` (section 3).
- Avalonia supports macOS; an unsigned build will trigger Gatekeeper warnings for other users.

## 6. Suggested order

1. Section 2 (server-only hardware proof, including `cycle_test.py`). Stop here if diversity is
   unreliable and decide whether that is acceptable.
2. Abstract audio output; add the cross-platform backend.
3. Abstract child-reaping and the service host; add systemd (Linux) / launchd (macOS) units.
4. Per-platform paths, launch scripts and setup notes.
5. Re-run the client against a Linux/macOS-hosted server **over the network first** (the client on
   Windows, the server elsewhere already works today via attach mode / the supervisor). That decouples
   "does the server run there" from "does the client run there".
