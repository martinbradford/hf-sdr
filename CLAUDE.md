# CLAUDE.md

Guidance for Claude Code (and humans) working in this repo.

## What this is

HF SDR application for the **SDRPlay RSP Duo** (HF only, 0–30 MHz; diversity
reception is a first-class feature). Two parts talking over **ZeroMQ**:
- `server/` — GNU Radio **3.10.x** headless Python server (all RF/DSP)
- `client/` — Avalonia **C#** client (UI) — not started yet

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
3. **Dual-tuner (diversity/independent) init is UNRELIABLE in gr-sdrplay3.** It inits cleanly only ~once per SDRConnect reset, then `sdrplay_api_Fail` (or a silent crash) and wedges the device. Not hardware — single-tuner is rock-solid and SDRConnect/SDRuno switch modes fine. Workaround: open & close SDRConnect once to reset the API, then run; if wedged, `Restart-Service SDRplayAPIService` (may need a physical USB power-cycle). Real fix = build gr-sdrplay3 from source / retry-reset wrapper (a **shack task**). In diversity mode use **single-form** setters (freq+gain); per-tuner/(A,B) freq setter **segfaults** (per-tuner gain is independent-RX only). Details: [`docs/SETUP_NOTES.md`](docs/SETUP_NOTES.md).
4. **For multiple receivers in one band, use single-tuner + multiple VRXs** (freq-xlating demod chains) — reliable, no dual-tuner needed. Dual-tuner independent RX is only for receivers on *different* bands.

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
4. **ZMQ / headless server** — IN PROGRESS.
   - Contract: [`protocol/messages.md`](protocol/messages.md) v0.1 (approved).
   - Server: `server/python/headless/server.py` — single-tuner capture,
     spectrum + multi-VRX audio over ZMQ, dynamic VRX via lock()/unlock().
     Validated end-to-end. Reference client: `example_client.py`.
   - **NEXT: the Avalonia C# client** (`client/`, not started) — NetMQ to the
     three sockets, render waterfall from float32 frames, play int16 audio,
     drive tuning/VRX via control. (C# build/run is on the VS side, not runnable
     from Claude Code here.)

Other reliable PoCs: `multi_vrx.py` (multiple in-band VRXs, single tuner),
`mode_switch.py` (HAL: single/diversity/independent source factory).

GRC for Stages 1–3; programmatic Python flowgraphs (`server/python/headless/`)
for Stage 4+. Commit both `.grc` and generated `.py`.

### How to run
- Server: `C:\Users\MABY\radioconda\python.exe server\python\headless\server.py --center 7.15e6`
  (then `example_client.py` to smoke-test). Ports: control 5555, stream 5556, audio 5557.
- Any PoC: `C:\Users\MABY\radioconda\python.exe server\python\poc\<script>.py --help`
- Testing DSP without hardware: construct blocks / run `test_combiner.py`.
  Hardware tests via Claude Code need `dangerouslyDisableSandbox: true`.

## Conventions

- Commit author identity: `martinbradford` / `martin.a.bradford@hotmail.co.uk`.
- Remote: https://github.com/martinbradford/hf-sdr (private, branch `main`).
