# HF SDR — Avalonia client

Minimal C# client that proves the [ZMQ architecture](../protocol/messages.md):
control (REQ/REP), spectrum waterfall (SUB), and audio playback (SUB).

**This is an architecture proof, not the finished UI** — one window: Connect,
a frequency box + Tune, and "Add VRX + Listen". It renders the waterfall from
the server's float32 frames and plays a VRX's int16 audio.

## Prerequisites

- .NET 10 SDK, Windows (audio uses NAudio/WinMM).
- Packages restore automatically: Avalonia 12, NetMQ, NAudio.

## Run

1. Start the server (on the shack PC with the RSP Duo):
   ```
   C:\Users\MABY\radioconda\python.exe ..\server\python\headless\server.py --center 7.15e6
   ```
2. Run the client:
   ```
   dotnet run --project HfSdr.App
   ```
   (or open `HfSdr.sln` in Visual Studio and F5). Defaults to `localhost`;
   edit `SdrClient.Connect(host)` for a remote server.
3. **Connect** → the waterfall should come alive (control handshake + spectrum
   stream). Type a frequency and **Tune**. Click **Add VRX + Listen** to hear
   the signal at the tuned frequency.

## What it exercises

| Leg | Socket | Proven by |
|-----|--------|-----------|
| Control | REQ/REP :5555 | `hello`/`get_status`/`set_center_freq`/`add_vrx` round-trips |
| Spectrum | SUB :5556 | live scrolling waterfall from `float32[fft_size]` dBFS frames |
| Audio | SUB :5557 | VRX `int16` audio played via NAudio |

## Layout

- `SdrClient.cs` — the whole ZMQ boundary (control + spectrum + audio).
- `WaterfallRenderer.cs` — float32 dBFS → scrolling BGRA bitmap.
- `MainWindow.axaml[.cs]` — the proof UI.

Later (per the design doc) this grows into `HfSdr.Core` (client library) +
`HfSdr.App` (UI) + `HfSdr.Tests`; kept as one project here for a fast proof.
