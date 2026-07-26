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

## Development stages (see context doc for detail)

1. Hardware proof — `server/python/poc/hardware_verify.py` (Qt spectrum + waterfall) ✅
2. SSB demod — `server/python/poc/ssb_demod.py` (`--freq/--center/--mode/--rf-gr/--agc`) ✅
3. **Diversity** — dual coherent streams → phase/amplitude correction → MRC combiner ← next, highest risk
4. ZMQ introduction — headless flowgraph + REQ/REP control; Avalonia client begins

GRC for Stages 1–3; programmatic Python flowgraphs (`server/python/headless/`) for Stage 4+. Commit both `.grc` and generated `.py`.

## Conventions

- Commit author identity: `martinbradford` / `martin.a.bradford@hotmail.co.uk`.
- Remote: https://github.com/martinbradford/hf-sdr (private, branch `main`).
