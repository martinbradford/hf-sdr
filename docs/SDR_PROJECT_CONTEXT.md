# SDR Project Context — HF SDR Application for RSP Duo

---

## For Claude Code — Read This First

This file primes you on a project that was designed in a Cowork session on a Surface laptop.
The immediate job on this Shack PC is **environment setup and repo creation** before any coding begins.
Work through the Setup Tasks section below in order. Do not skip steps — the sequence matters.

---

## Setup Tasks (do these now, in order)

### 1. Create the repository structure on disk

In the user's repositories directory, create this folder tree:

```
hf-sdr/
├── docs/
├── server/
│   ├── flowgraphs/
│   └── python/
│       ├── poc/
│       └── headless/
├── protocol/
└── client/
```

Move this file into `hf-sdr/docs/SDR_PROJECT_CONTEXT.md` once the structure exists.

### 2. Create a .gitignore

Create `hf-sdr/.gitignore` covering:
- Python: `__pycache__/`, `*.pyc`, `*.pyo`, `.ipynb_checkpoints/`
- GNU Radio: `*.pyc` generated alongside `.grc` files
- Conda: `envs/`, `pkgs/`, `.conda/` — do NOT commit the radioconda environment
- Visual Studio: standard VS `.gitignore` entries (bin/, obj/, .vs/, *.user, *.suo)
- Avalonia: nothing extra beyond standard VS entries
- OS: `.DS_Store`, `Thumbs.db`, `desktop.ini`

### 3. Initialise git and create the GitHub repository

- `git init` in `hf-sdr/`
- Create an initial commit with the folder structure and .gitignore
- Create a new GitHub repository (ask the user for the preferred repo name and whether it should be public or private — it will eventually be open source but may start private)
- Push and set upstream

### 4. Reinstall radioconda (GNU Radio environment)

The existing radioconda install (GNU Radio 3.10.10.0) is not upgrading cleanly. Do a fresh install.

**Sequence is critical:**

1. Uninstall existing radioconda fully (via Windows Add/Remove Programs, then delete any leftover folders)
2. Download the latest radioconda installer from https://github.com/ryanvolz/radioconda/releases/
3. Install radioconda — this should give GNU Radio 3.10.12
4. Verify: open a radioconda prompt and run `python -c "import gnuradio; print(gnuradio.__version__)"`

### 5. Install the SDRPlay API service

**Must be done before gr-sdrplay3 — order matters.**

- Download the latest SDRPlay API installer from https://www.sdrplay.com/downloads/
- Install it — it runs as a Windows service (`SDRplay API Service`)
- Verify the service is running in Windows Services before proceeding

### 6. Install gr-sdrplay3

- In a radioconda prompt: `conda install -c conda-forge gr-sdrplay3`
- Verify the package version is compatible with the installed SDRPlay API version
- Reference for any issues: https://github.com/fventuri/gr-sdrplay3/blob/main/WINDOWS.md

### 7. Verify the environment with a minimal flowgraph

Create `server/python/poc/hardware_verify.py` — a minimal Python GNU Radio flowgraph that:
- Instantiates an RSPduo source (single tuner, not diversity yet)
- Connects it to a QT GUI frequency sink and waterfall sink
- Runs for 30 seconds then exits

This is Stage 1 of the development plan. If the spectrum looks sane on a known HF band (e.g. 7 MHz, 40m), the environment is good and development can proceed.

---

## Project Overview

Building a sophisticated HF SDR application in two parts:

1. **GNU Radio headless server** (Python) — handles all RF and DSP
2. **Avalonia C# client** — UI, display, and user interaction

IPC between them via **ZeroMQ** (gr-zeromq on the server, NetMQ in C#).

Target OS: **Windows 11 for both server and client**. Windows-first design — no cross-platform compromises. Open source, so others may port it, but the design won't be bent to accommodate them.

---

## Hardware

**SDRPlay RSP Duo** — dual-tuner, phase-coherent HF SDR.

- Frequency range of interest: HF only (0–30MHz, including MW/LW)
- **Diversity reception is a first-class feature**, not an afterthought — design everything around it from day one
- The RSP Duo has two antenna inputs (Antenna A SMA, Antenna B SMA, Hi-Z port for wire antennas) — the two inputs for diversity may be different impedance types, which affects phase/amplitude correction
- Future: possibly a second RSP device (RSP1B or RSP2) for VHF/UHF on a separate instance — no diversity needed for that, single tuner only

---

## Technology Stack

| Component | Choice | Notes |
|-----------|--------|-------|
| GNU Radio | 3.10.12 via radioconda | **Do NOT use GR4** — RC1 only, no OOT ecosystem yet |
| RSP Duo driver | gr-sdrplay3 (fventuri) | conda-forge; needs SDRPlay API v3 service installed first |
| ZMQ (server) | gr-zeromq | Bundled with GNU Radio |
| ZMQ (client) | NetMQ | C# NuGet package |
| UI framework | Avalonia | Cross-platform C# UI |
| IDE (client) | Visual Studio (full, Community edition) | Better Avalonia designer than VS Code |
| Demodulation | GNU Radio blocks | All DSP stays server-side |

**GR4 warning:** GNU Radio 4.0 RC1 exists (March 2026) but is a complete architectural rewrite with no OOT module support (gr-sdrplay3 not ported). Avoid for this project — 3.10.12 is the correct choice for the foreseeable future.

---

## Architecture Decisions

### Repository structure

```
hf-sdr/                         ← repo root
├── .gitignore
├── README.md
├── docs/
│   └── SDR_PROJECT_CONTEXT.md  ← this file
├── server/                     ← GNU Radio side
│   ├── flowgraphs/             ← .grc files (PoC and development)
│   └── python/
│       ├── poc/                ← Stage 1–3 proof of concept scripts
│       └── headless/           ← ZMQ server (Stage 4+)
├── protocol/                   ← ZMQ message contract (shared boundary)
│   └── messages.md
└── client/                     ← Avalonia C# side
    ├── HfSdr.sln
    ├── HfSdr.App/
    ├── HfSdr.Core/
    └── HfSdr.Tests/
```

### ZMQ Boundary (what crosses the IPC)

The GNU Radio server sends **processed data**, not raw IQ:

- **FFT magnitude arrays** (float32[]) → waterfall and spectrum display in client
- **Demodulated audio samples** → streamed to client for playback
- **Control channel** (ZMQ REQ/REP) → client sends tuning commands to server

### Control Channel Protocol

Minimum command set:
- Center frequency, sample rate / bandwidth, RF and IF gain
- Demodulator mode (LSB, USB, CW, AM, NFM)
- Passband tuning / IF shift, squelch
- Noise blanker on/off, notch filter (manual and auto)

Encoding: start with length-prefixed JSON over ZMQ REQ/REP. Can migrate to Protobuf later.

### Hardware Abstraction Layer

Implement a thin HAL in the GNU Radio server from the start. Adding a single-tuner device later should mean adding a new source implementation, not restructuring the flowgraph.

### Diversity Reception

gr-sdrplay3 RSPduo diversity source outputs two coherent IQ streams:

```
RSPduo Diversity Source
    ├── Stream 0 (Antenna A) ──┐
    └── Stream 1 (Antenna B) ──┴── Phase/Amplitude Correction → MRC Combiner → Demod chain
```

Maximal Ratio Combining (MRC) preferred over Equal Gain Combining (EGC).

---

## Feature Priorities (HF-focused)

**Build first:** SSB (LSB/USB), CW, AM, noise blanker, manual notch filter, passband tuning / IF shift, dual waterfall

**Build soon:** Automatic notch filter, NFM, AGC (HF time constants), IQ recording, audio recording, frequency memories

**Lower priority:** WFM, RDS decoding, scanning

---

## Development Stages

### Stage 1 — Hardware Proof ← START HERE (after environment setup)
`RSPduo Source → QT GUI Spectrum Sink + QT GUI Waterfall Sink`
Proves RSP Duo talks to gr-sdrplay3 and IQ samples are flowing.

### Stage 2 — Demodulation Proof
`Frequency Translator → Decimation → SSB Demodulator → Audio Sink`
Tune to 40m SSB. Verify audio sounds correct.

### Stage 3 — Diversity Proof
Switch to RSPduo diversity source. Add phase/amplitude correction and MRC combiner.
Highest technical risk — prove before building around it.

### Stage 4 — ZMQ Introduction
Replace Qt sinks with ZMQ sinks. Add REQ/REP control channel.
Flowgraph becomes headless-capable. IPC contract defined. Avalonia client development begins.

---

## Note on GRC vs Programmatic Flowgraphs

GNU Radio Companion (GRC) is right for Stages 1–3. For Stage 4 and beyond, move to programmatic Python flowgraphs (GNU Radio Python API directly). The `lock()`/`unlock()` pattern for dynamically adding/removing receiver chains is not practical to express in GRC. The `server/python/headless/` folder is where programmatic flowgraphs live.

Keep both `.grc` files and their generated Python under version control.

---

*Context generated from design discussion, 2026-07-26. Shack PC setup is the immediate next step.*
