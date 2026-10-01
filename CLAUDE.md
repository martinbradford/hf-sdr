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
     Server lifecycle (client spawns + supervises the server; why *not* to embed it):
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

## Conventions

- Commit author identity: `martinbradford` / `martin.a.bradford@hotmail.co.uk`.
- Remote: https://github.com/martinbradford/hf-sdr (private, branch `main`).
