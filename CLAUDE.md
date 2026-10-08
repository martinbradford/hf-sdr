# CLAUDE.md

Guidance for Claude Code (and humans) working in this repo.

## What this is

HF SDR application for the **SDRPlay RSP Duo** (HF only, 0–30 MHz; diversity
reception is a first-class feature). Two parts talking over **ZeroMQ**:
- `server/` — GNU Radio **3.10.x** headless Python server (all RF/DSP)
- `client/` — Avalonia **12** / **.NET 10** client (UI) — working architecture proof

**Read these first:**
- [`docs/SDR_PROJECT_CONTEXT.md`](docs/SDR_PROJECT_CONTEXT.md) — full design & architecture
- [`docs/SETUP_NOTES.md`](docs/SETUP_NOTES.md) — environment, machine-specific gotchas, corrections to the context doc
- [`docs/PORTING.md`](docs/PORTING.md) — Linux/macOS assessment (nothing run off Windows yet); read before any non-Windows work
- [`protocol/bandwidth_design.md`](protocol/bandwidth_design.md) — exploratory, low priority: reducing stream bandwidth (Wi-Fi/remote); also documents the audio lost/late/dry metrics
- [`README.md`](README.md) — overview + live status

## Environment (Windows 11, "Shack PC" with the RSP Duo attached)

- GNU Radio 3.10.12 via radioconda at `C:\Users\MABY\radioconda`. **Do NOT use GNU Radio 4.x** (no OOT ecosystem; gr-sdrplay3 not ported).
- Run Python with the radioconda interpreter directly:
  ```
  C:\Users\MABY\radioconda\python.exe <script>
  ```
- Driver: `gnuradio-sdrplay3` (module `gnuradio.sdrplay3`). It is **not on any conda channel** — installed from the prebuilt `.conda` on fventuri's GitHub releases. SDRPlay API service must be running.

## Critical gotchas

1. **Norton breaks TLS.** Norton intercepts HTTPS on this PC.
   - conda: point it at the exported Windows CA bundle — already set via
     `conda config --set ssl_verify C:\Users\MABY\radioconda\win-trusted-ca.pem`. Never use `ssl_verify: false`.
   - git: `git config http.sslBackend schannel` (uses Windows cert store). Already set on this repo.
   - When running installs/network via Claude Code's Bash/PowerShell tools, use `dangerouslyDisableSandbox: true` (the sandbox proxy adds its own untrusted TLS layer on top of Norton's).
2. **gr-sdrplay3 gain: mind the SIGN.** SDRPlay speaks *gain reduction* (positive dB, higher = less gain: IF `[20-59]`, RF `[0-61]` on HF). But **gr-sdrplay3's `set_gain`/`get_gain`/`get_gain_range` use the _negative_ of that ("gain": IF `(-59,-20)`, RF `(-61,0)`)**. So to apply *N dB of reduction* you must call `set_gain(-N, "IF"/"RF")`. Passing a positive value is out of range → `sdrplay_api_OutOfRange`, silently rejected, gain stuck at max → ADC overload. The headless server stores/reports positive reduction (protocol convention) and negates at the driver boundary (`_apply_gain`), clamping to the live `get_gain_range`. Enable IF AGC (`set_gain_mode(True)` + `set_agc_setpoint(-30)`) to avoid overload; raise RF reduction for strong signals. RF maps to discrete LNA steps — see gotcha #5.
3. **Dual-tuner init needs a clean in-process deinit — now largely SOLVED.** A fresh dual-tuner init fails (`sdrplay_api_Fail`/silent crash + wedges device) unless the previous source was properly *deinitialised* first. The headless server's `set_tuner_mode` does stop → `disconnect_all` → **`gc.collect()`** (forces the gr-sdrplay3 source destructor → device deinit) → rebuild → verify samples flow → retry 3× → fall back to single on failure. This makes **live single↔diversity switching work repeatably** (occasional fails degrade gracefully). The standalone PoC scripts (`diversity_rx.py`, `mode_switch.py`) do NOT do this, so they still need the old workaround: open & close **SDRConnect** once to reset the API; if wedged, `Restart-Service SDRplayAPIService` or a physical USB power-cycle. Not hardware — single-tuner is rock-solid; SDRConnect/SDRuno switch fine. In diversity the two tuners are **linked**: use the **single-form** freq _and_ gain setters (they drive both). Per-tuner / (A,B) forms are for **independent** mode only — in diversity they misbehave: per-tuner freq **segfaults**, and per-tuner gain get/set throws `get_independent_rx_channel_params() … not in independent RX mode` and **crashes the process**. So there is no per-channel gain to tune in diversity (don't try to "fix" AGC per tuner); a hashy diversity waterfall is usually just RF/LNA overload — raise `rf_gr_db`. AGC only controls IF, never RF/LNA. Details: [`docs/SETUP_NOTES.md`](docs/SETUP_NOTES.md).
4. **For multiple receivers in one band, use single-tuner + multiple VRXs** (freq-xlating demod chains) — reliable, no dual-tuner needed. Dual-tuner independent RX is only for receivers on *different* bands.
5. **RF gain = discrete LNA steps — SOLVED (was the "diversity overloads" bug).** IF is continuous `[20-59]`. RF reduction is a set of discrete, band-limited **LNA states** — on HF, states 0–6 = `{0,6,12,18,37,42,61}` dB. The root cause of the overload was gotcha #2's sign flip (server sent *positive* `rf_gr_db` → OutOfRange → RF stuck at state 0 = max gain). Fixed: the server negates + clamps to `get_gain_range("RF")`, and gr-sdrplay3 **auto-snaps** the requested dB to the nearest valid LNA step. `set_gain`/`get_status` echo the actual applied `rf_gr_db`, the resulting `lna_state`, and `rf_gr_db_range`/`if_gr_db_range` for the current band. Verify with `ctl.py set_gain rf_gr_db=30` → replies `rf_gr_db:37, lna_state:4`. (The standalone PoCs still pass raw positive values — same latent bug, not yet fixed there.)

## Development stages / current state

1. **Hardware proof + live monitor** — `server/python/poc/hardware_verify.py`
   (Qt spectrum + waterfall + SSB audio; ▼/▲ tuning 100k/5k/500Hz, LSB/USB,
   `--span-khz` zoom) ✅
2. **SSB demod** — `server/python/poc/ssb_demod.py`
   (`--freq/--center/--mode/--rf-gr/--if-gr/--agc/--bw`) ✅
3. **Diversity** — `server/python/poc/diversity_rx.py`: dual-tuner source →
   `DiversityCombiner` (phase/amp align + MRC) → SSB → audio. Combiner proven
   (unit test `test_combiner.py`; decoded FT8 on hardware). ⚠️ dual-tuner init
   unreliable (gotcha #3) — real combining-gain measurement is a shack task.
4. **ZMQ / headless server + client** — WORKING END TO END.
   - Contract: [`protocol/messages.md`](protocol/messages.md) v0.1.
     Server lifecycle (client spawns + supervises the server; why *not* to embed it,
     and why **ControlPort/Thrift is a dead end** — no Thrift backend in radioconda,
     can't build a flowgraph, gr-sdrplay3 registers no knobs):
     [`protocol/server_lifecycle.md`](protocol/server_lifecycle.md).
   - Server: `server/python/headless/server.py` — capture + spectrum + multi-VRX
     audio over ZMQ; dynamic VRX via lock()/unlock(); **live tuner-mode switching
     single↔diversity over the control channel** (`set_tuner_mode`; see gotcha #3).
     **Targeted diversity null** (`null_signal`): point at an interferer's
     frequency and the server band-isolates both branches there, estimates the
     cancelling weight `w = h0/h1`, and applies `y = x0 − w·x1` across the whole
     capture — a DSP phasing canceller (like an MFJ-1026/NCC-1, but computed
     from where you point instead of tuned by ear). Auto-tracks by default;
     `track:false` freezes; `track_speed` (fast/med/slow) sets the adaptation
     time constant; `amp`/`phase_deg` hand-trim; `clear:true` disengages.
     Reports the measured `null_depth_db`. Fade-robust for distant skywave
     sources: during a deep branch-B fade it holds the last weight instead of
     dividing into the noise, regularises the denominator, and caps `|w|` (an
     un-gated weight otherwise wanders wildly on a fading source — use `slow`).
     Offline proof: `test_null.py` (no hardware — 58 dB cancel on a stable
     source, wanted signal untouched; plus a fading case that stays bounded).
     `ctl.py` = tiny control CLI; `example_client.py` = smoke test.
   - Client: `client/HfSdr.App` (Avalonia 12 / .NET 10) — waterfall with a
     **frequency scale** across the top (`FrequencyScale.cs`: 1/2/5 × 10ⁿ tick
     step auto-chosen from the live span, minor ticks, MHz labels, yellow caret
     at the tuned VRX; shares the waterfall's Hz→x mapping), **click-to-tune
     + mouse-wheel fine tuning** (50 Hz / Ctrl 10 Hz / Shift 500 Hz), LSB/USB,
     int16 audio via NAudio. **Audio-output device selector** (WASAPI
     `WasapiOut` — full endpoint names, e.g. a VB-Audio virtual cable to route
     into WSJT-X; switchable live; device list captured at startup).
     **Gain controls** (AGC / RF-gr slider that snaps to LNA steps + shows
     `lna_state` / IF-gr slider), **single↔diversity tuner radios** (locks UI
     during the ~seconds switch, re-adopts the restored VRX, reverts on
     dual-tuner init failure), and a **peak / OVERLOAD readout** (green/amber/red)
     fed by `peak_dbfs`/`overload` in the spectrum header. **Diversity-null UI**
     (right-click a waterfall signal → "Null this source" / "Clear null"): auto-
     sizes the target band from the spectrum (−10 dB width), shades the nulled
     band on the waterfall, polls `get_status` to animate `null_depth_db`, and
     offers Track (freeze/thaw), a Fast/Med/Slow tracking-speed selector, and
     amp/phase manual-trim sliders. Diversity-only (the bar disables in single).
     Proven end-to-end on real signals (decoded FT8).
     `dotnet build` works from here (SDK installed); GUI *run* needs a desktop.
     RF gain validation is fixed (gotcha #5): `set_gain` negates+clamps and
     snaps RF to valid LNA steps; status reports `lna_state` + valid ranges.
     Overload is measured from a raw-stream peak tap (gr-sdrplay3 has no
     overload message port; fc32 full scale = ADC full scale).
   - **WORKING — twin-PC operation (client and RSP on different PCs); formal §12.9 tests still to run.** Proven 2026-10-07: laptop client → `new-shack-pc.local` supervisor → live waterfall + audio. Design:
     [`protocol/server_lifecycle.md`](protocol/server_lifecycle.md) **§12**. Plan: a tiny
     always-on **supervisor Windows service** (.NET Worker Service, port 5554,
     `status`/`start`/`stop`) that launches/stops the Python server on demand — the
     *server* is deliberately NOT a service (it would hold the RSP). Home LAN + VPN only.
     - **Done & validated on the Shack PC (code in commit `acc5421`):**
       server `shutdown` command (`--shutdown-token` / `HF_SDR_SHUTDOWN_TOKEN`), all three
       sockets bound before capture starts (port clash → exit 2), `--bind` (default `*`).
       Offline test: `server/python/headless/test_lifecycle.py` (stubs GNU Radio if absent) —
       ALL PASS. Hardware: a second instance exits immediately on the port clash with the
       first unaffected; `ctl.py shutdown token=test` → `stopping: true`; immediate restart
       re-opened the RSP cleanly on every repeat, in both **single** and **diversity**
       (switched with `set_tuner_mode mode=diversity` before each shutdown). Use a
       non-numeric, non-bool token because `ctl.py` JSON-parses values. (§12.9 criteria met.)
     - **Supervisor service (`supervisor/`) — installed as a Windows service (`HfSdrSupervisor`, LocalSystem, Automatic-Delayed) and validated on the Shack PC:**
       state machine, Job Object, graceful stop via `shutdown`, `status`/`start`/`stop` on :5554. Real RSP under LocalSystem works (§12.6 passed — no account change needed);
       10/10 `start`/`stop` cycles in single and 10/10 in diversity; `Stop-Service` with the server running shuts it down gracefully and the RSP re-opens cleanly.
       Install: `dotnet publish supervisor\HfSdr.Supervisor -c Release -o C:\ProgramData\hf-sdr\supervisor` then `New-Service` (see `supervisor/README.md`); republishing needs the service stopped (admin).
       Gotchas hit: `supervisor.json` paths must use `/` (or doubled `\\`) — bare backslashes are invalid JSON and the service dies at startup.
       **Still TODO (§12.9, at the machine):** SDRConnect opens the RSP right after `stop`; reboot with nobody logged in → reachable on 5554; start while SDRConnect holds the RSP → `failed` with real `log_tail`; firewall scope 5554–5557 (home + VPN subnets only).
     - **Client `supervisor` launch mode — written, builds, NOT yet exercised in the GUI:** `SupervisorClient.cs` (throwaway REQ socket per call = §4.3 for the supervisor path),
       `ClientSettings.cs` (host / use-supervisor / release-on-close in `%APPDATA%\HfSdr\settings.json`), and `MainWindow` Host box, "Start via supervisor", "Release receiver on close"
       (opt-in, default off), "Stop receiver" (stop + disconnect). Connect flow = §12.7: status → start{center,tuner_mode} → poll → attach using the ports the supervisor reports.
     - **Audio loss/lateness metrics — written, builds, logic unit-checked offline, NOT yet run against a live server:**
       client readout `audio lost/late/dry/resync` (`AudioStats.cs`, tooltip + `%APPDATA%\HfSdr\audio-events.log`) and server
       `get_status` → `streaming.dropped_frames`. TODO at the machines: connect over Wi-Fi, confirm the readout stays
       0/0/0/0 on a clean link, then watch it during real dropouts (see `protocol/bandwidth_design.md` §6).
       First colocated run showed `dry` was mostly start-up and sub-ms noise, so the metric was tightened and a
       playback jitter cushion added (`AudioPrimeMs`, default 80 ms, in `settings.json`; costs that much latency).
       **Wi-Fi results (2026-10-08, laptop → shack PC, ~10 min runs):** lost 0 and resync 0 throughout; the link
       delivers late, not lossy (stalls 100–285 ms in bursts). Cushion 250 ms gave `dry 0`; 0 and 80 gave a handful of
       dry reads while the buffer ratcheted up to ~250 ms on its own. **Use `AudioPrimeMs` 250 on Wi-Fi clients, keep 80
       for colocated (provisional for a wired LAN client).** Stall source is the **network path between the PCs, not the sender**:
       a client on the shack PC itself (`localhost`) ran >15 min with lost/late/dry/resync all 0. The laptop link is excellent with
       no roams; pinning it to the mesh master (wired uplink, roaming off) cut late events (12 vs 49 per ~10 min) but did not remove
       them, so the wireless backhaul is not the main cause. Exact hop not found. **Next test: a wired client elsewhere in the house
       → shack PC for ~15 min** (zero `late` = Wi-Fi is the cause; stalls remain = switch/pfSense/shack NIC). Not worth chasing for
       daily use: 250 ms already makes the laptop's audio smooth. Details: `protocol/bandwidth_design.md` §6.1.
       The laptop's `settings.json` currently has 250. `audio-events.log` is only written when something happens, so a clean run
       leaves no file (a session start/end line would fix that; not implemented).
     - **Gapless USB/LSB (and CW) switching — written, builds, logic unit-tested offline, NOT yet run against GNU Radio or hardware:**
       a sideband change used to be client-side `remove_vrx` + `add_vrx`, i.e. two flowgraph `lock()`/`unlock()` reconfigurations (the
       multi-second gap). lsb/usb/cw share one demod chain and differ only in the band-pass taps, so `update_vrx` now accepts `mode`/`filter`
       and swaps the taps on the running filter (`server.py`: `resolve_demod_change`, `apply_demod_change`, `IN_PLACE_MODES`); the client calls
       `update_vrx` (falls back to remove + add for an old server or a mode needing another demodulator) and applies the dropdown immediately.
       AM/FM (not implemented) would still need remove + add. **TODO at the shack PC:** run `test_sideband_flowgraph.py` (real GNU Radio, no RSP;
       checks audio follows the sideband, no gap, contiguous seq, and prints the click size at the switch), then listen to a real signal. If the
       click is objectionable the upgrade is a sample-aligned crossfade between two parallel filters (≈2× filter CPU per VRX); a volume ramp from
       the control thread was rejected because it cannot be sample-aligned through GNU Radio's buffers. Also unverified: that `set_taps` on a
       running `fir_filter_ccc` is safe (believed so; the test exercises it). Offline: `test_vrx_update.py`.
     - **Done:** lazy-pirate in `SdrClient.Send` (a timed-out control request now replaces the wedged REQ socket; not retried). **Not started:** §4.4 client-side Job Object (only needed for local `spawn` mode).
     - Open check for the service: run as LocalSystem first (§12.6) — verify it can read
       radioconda and import `gnuradio.sdrplay3`; else log on as the owner's account.
   - **NEXT:** the **101Cats integration is the priority** — see *Product direction*
     below and [`protocol/integration_design.md`](protocol/integration_design.md).
     Also: productionise the client (MVVM refactor, single-VRX → multi-VRX,
     persistence); characterise switch reliability (clean single↔diversity flips
     from a power-cycle); two-antenna diversity-gain measurement (shack); apply
     the same gain-sign fix to the PoCs. (Done: audio-device selector, RF/IF gain
     fix, client gain + tuner-mode controls, overload metering, server-side
     targeted diversity null + client right-click null UI.)

Other reliable PoCs: `multi_vrx.py` (multiple in-band VRXs, single tuner),
`mode_switch.py` (HAL: single/diversity/independent source factory).
Note: audio latency over LAN is high (jitter); local is fine (~0.5s FT8 DT).

GRC for Stages 1–3; programmatic Python flowgraphs (`server/python/headless/`)
for Stage 4+. Commit both `.grc` and generated `.py`.

### How to run
- Server: `C:\Users\MABY\radioconda\python.exe server\python\headless\server.py --center 7.15e6`
  Ports: control 5555, stream 5556, audio 5557. Ctrl-C stops it (fixed on Windows).
- Client GUI (desktop only): `dotnet run --project client\HfSdr.App` — Connect,
  click the waterfall to tune, wheel to fine-tune.
- Control CLI: `python server\python\headless\ctl.py get_status` |
  `... ctl.py set_tuner_mode mode=diversity` | `... ctl.py set_gain rf_gr_db=0` |
  `... ctl.py null_signal center_hz=14005000 width_hz=12000` (diversity: null a
  source; watch `null_depth_db` climb) | `... ctl.py null_signal clear=true`
- Reliability cycles (both need hardware; not the same thing):
  `server\python\headless\cycle_test.py --cycles 10` flips **single↔diversity** on a running
  server and checks the mode and that spectrum frames flow (written but not yet run on
  hardware, so no baseline); `supervisor\tools\cycle.py <single|diversity> <n>` cycles the
  **supervisor's start/stop** (the server being launched and shut down).
- Any PoC: `C:\Users\MABY\radioconda\python.exe server\python\poc\<script>.py --help`
- DSP without hardware: run `test_combiner.py`. `dotnet build client\HfSdr.App`
  compiles the client here. Hardware tests via Claude Code need `dangerouslyDisableSandbox: true`.

## Product direction & 101Cats integration

The end goal is a **personal replacement for SDRPlay's SDRConnect** that meets the
owner's needs, with **tight integration to 101Cats** (his Yaesu FTdx101D CAT
controller at `D:\RiderProjects\Avalonia101Cats`) so the SDR + transceiver feel
like *one* operating environment. Operating reality: the RSP Duo is often the
**primary receiver** (101D receiver off, 101D used for TX); it has a flat response
good for FT8. Decision: **two separate, closely-integrated apps** — do NOT merge
the SDR into 101Cats (would bloat it for the many 101Cats users with no RSP).

Client strategy: **evolve** the Avalonia client (MVVM refactor, multi-VRX,
persistence), don't rewrite — the real architecture is the server/ZMQ split and
the client is a thin, replaceable view.

**Integration mechanism — over ZeroMQ, not CAT-over-COM.** 101Cats becomes a
second ZMQ peer of the SDR server. Design note: [`protocol/integration_design.md`](protocol/integration_design.md);
background + SDRPlay's own mechanisms & SDR Uno quirks: [`Integration.MD`](Integration.MD).
Core model: **the SDR is the VFO; the 101D follows.** One **rig-linked VRX** at a
time (others are free monitors); default direction **rig-follows-SDR** via a pushed
`linked_tuned` event (no polling); "park on a DX" = unlink; TX-aware mute; single
operating VFO. Needs a control-channel `ROUTER` upgrade (GUI + 101Cats = two
clients). SDRConnect's WebSocket property API is a *separate, optional*
compatibility surface for other tools — not the 101Cats path.

**IMPORTANT:** the 101Cats source tree is currently **destabilised** — do NOT edit
it without first checking it is healthy. Read-only reference is fine.

## Working from another PC (laptop)

Client-only work (Avalonia/.NET) can be done on any PC with the .NET 10 SDK: `dotnet build client\HfSdr.App`. The server, supervisor and RSP live on the Shack PC
(`new-shack-pc.local`; supervisor service `HfSdrSupervisor` on :5554 is always on). To run the client against it: `dotnet run --project client\HfSdr.App`, set Host to
`new-shack-pc.local`, tick "Start via supervisor", Connect. Settings persist in `%APPDATA%\HfSdr\settings.json`. Hardware/server paths in the rest of this file (radioconda,
`D:\Repos\HF-SDR`) refer to the Shack PC. Supervisor test helpers: `supervisor/tools/` (set `HF_SDR_HOST`). Pending: Martin's client-enhancements list (an MD file in the repo).
Server supports several simultaneous clients only loosely (shared state, no ownership, client plays all `audio/` topics) — see the ROUTER upgrade in `protocol/integration_design.md`.

## Conventions

- Commit author identity: `martinbradford` / `martin.a.bradford@hotmail.co.uk`.
- Remote: https://github.com/martinbradford/hf-sdr (private, branch `main`).
