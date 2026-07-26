# HF SDR — RSP Duo

A sophisticated HF software-defined radio application for the **SDRPlay RSP Duo**,
built as two cooperating parts:

- **GNU Radio headless server** (Python) — all RF and DSP
- **Avalonia C# client** — UI, display, and user interaction

IPC between them is over **ZeroMQ** (`gr-zeromq` server-side, NetMQ client-side).
Diversity reception is a first-class feature, designed in from day one.

> Full design rationale lives in [`docs/SDR_PROJECT_CONTEXT.md`](docs/SDR_PROJECT_CONTEXT.md).

## Layout

```
hf-sdr/
├── docs/       design context and notes
├── server/     GNU Radio side
│   ├── flowgraphs/   .grc files (PoC + development)
│   └── python/
│       ├── poc/       Stage 1–3 proof-of-concept scripts
│       └── headless/  ZMQ server (Stage 4+)
├── protocol/   ZMQ message contract (shared boundary)
└── client/     Avalonia C# side
```

## Environment (Windows 11)

| Component        | Version / Source                                   |
|------------------|----------------------------------------------------|
| GNU Radio        | 3.10.12 via [radioconda](https://github.com/ryanvolz/radioconda/releases/) |
| RSP Duo driver   | `gr-sdrplay3` (conda-forge)                         |
| SDRPlay API      | v3 service (must be installed & running first)      |
| Client UI        | Avalonia (C#), built in Visual Studio Community     |

**Do not use GNU Radio 4.x** — no OOT ecosystem yet (`gr-sdrplay3` not ported).

## Status

- [x] radioconda / GNU Radio 3.10.12 installed (Python 3.12.9)
- [x] SDRPlay API service running
- [x] `gnuradio-sdrplay3` 3.11.0.8 installed (see [docs/SETUP_NOTES.md](docs/SETUP_NOTES.md))
- [x] Stage 1 — hardware proof: IQ confirmed flowing from RSP Duo (headless smoke test)
- [ ] Stage 1 — visual confirmation (`server/python/poc/hardware_verify.py`, Qt waterfall)

## Development stages

1. **Hardware proof** — RSPduo source → Qt spectrum + waterfall sinks
2. **Demodulation proof** — SSB demod → audio
3. **Diversity proof** — dual coherent streams → phase/amplitude correction → MRC
4. **ZMQ introduction** — headless flowgraph + REQ/REP control; Avalonia client begins
