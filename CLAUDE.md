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
2. **gr-sdrplay3 gain is gain _reduction_ in dB** (higher = less gain): IF `[20-59]` (default 40), RF `[0..]` (default 0). Negative values clamp to min reduction = max gain → ADC overload. Enable IF AGC (`set_gain_mode(True)` + `set_agc_setpoint(-30)`) to avoid overload; raise RF reduction for strong signals.
3. **Dual-tuner init needs a clean in-process deinit — now largely SOLVED.** A fresh dual-tuner init fails (`sdrplay_api_Fail`/silent crash + wedges device) unless the previous source was properly *deinitialised* first. The headless server's `set_tuner_mode` does stop → `disconnect_all` → **`gc.collect()`** (forces the gr-sdrplay3 source destructor → device deinit) → rebuild → verify samples flow → retry 3× → fall back to single on failure. This makes **live single↔diversity switching work repeatably** (occasional fails degrade gracefully). The standalone PoC scripts (`diversity_rx.py`, `mode_switch.py`) do NOT do this, so they still need the old workaround: open & close **SDRConnect** once to reset the API; if wedged, `Restart-Service SDRplayAPIService` or a physical USB power-cycle. Not hardware — single-tuner is rock-solid; SDRConnect/SDRuno switch fine. In diversity use **single-form** freq setter (per-tuner/(A,B) freq **segfaults**). Details: [`docs/SETUP_NOTES.md`](docs/SETUP_NOTES.md).
4. **For multiple receivers in one band, use single-tuner + multiple VRXs** (freq-xlating demod chains) — reliable, no dual-tuner needed. Dual-tuner independent RX is only for receivers on *different* bands.
5. **RF gain reduction must be a VALID discrete LNA step** (OPEN BUG). IF is `[20-59]` (continuous). RF/LNA steps are discrete and band-limited — on HF, values like 40/90 raise `sdrplay_api_Update … OutOfRange` and the API **rejects the whole gain update silently** (RF stays put, so overload never clears). This is why diversity currently overloads (RF stuck at 0). TODO: query gr-sdrplay3 for valid RF steps and clamp/validate in the server (and client).

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
   - Server: `server/python/headless/server.py` — capture + spectrum + multi-VRX
     audio over ZMQ; dynamic VRX via lock()/unlock(); **live tuner-mode switching
     single↔diversity over the control channel** (`set_tuner_mode`; see gotcha #3).
     `ctl.py` = tiny control CLI; `example_client.py` = smoke test.
   - Client: `client/HfSdr.App` (Avalonia 12 / .NET 10) — waterfall, **click-to-tune
     + mouse-wheel fine tuning** (50 Hz / Ctrl 10 Hz / Shift 500 Hz), LSB/USB,
     int16 audio via NAudio. **Audio-output device selector** (WASAPI
     `WasapiOut` — full endpoint names, e.g. a VB-Audio virtual cable to route
     into WSJT-X; switchable live; device list captured at startup). Proven
     end-to-end on real signals (decoded FT8).
     `dotnet build` works from here (SDK installed); GUI *run* needs a desktop.
   - **NEXT:** fix RF gain validation (gotcha #5); add mode+gain controls to the
     client; CAT/rig control (e.g. Hamlib rigctld) so WSJT-X logs the real freq;
     characterise switch reliability (clean single↔diversity flips from a
     power-cycle); two-antenna diversity-gain measurement (shack).

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
  `... ctl.py set_tuner_mode mode=diversity` | `... ctl.py set_gain rf_gr_db=0`
- Any PoC: `C:\Users\MABY\radioconda\python.exe server\python\poc\<script>.py --help`
- DSP without hardware: run `test_combiner.py`. `dotnet build client\HfSdr.App`
  compiles the client here. Hardware tests via Claude Code need `dangerouslyDisableSandbox: true`.

## Conventions

- Commit author identity: `martinbradford` / `martin.a.bradford@hotmail.co.uk`.
- Remote: https://github.com/martinbradford/hf-sdr (private, branch `main`).
