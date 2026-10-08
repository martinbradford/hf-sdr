# Bandwidth Design Note: reducing stream bandwidth for Wi-Fi and remote use

**Status:** Exploratory proposal `v0.1-draft`. Nothing here is implemented, and it is **low
priority**. It extends [`messages.md`](messages.md) v0.1 without changing it: every addition is
opt-in, so existing clients keep today's behaviour. Figures below are **computed from the code**
(`server.py`, `messages.md`), not measured on the wire.

## 1. Why

Over a LAN the server currently sends about 3 Mbps to the client. That works, and on the owner's
setup (laptop client on Wi-Fi) dropouts are rare and confined to audio; the waterfall is fine. Two
reasons to care anyway:

1. **Wi-Fi jitter.** Occasional audio glitches when the wireless LAN gets busier. Lower bandwidth
   reduces exposure, though a deeper client jitter buffer may be the more effective fix (§6).
2. **Remote access.** If the receiver is ever used over the internet (via the owner's VPN), 3 Mbps
   is a poor fit for a home uplink and for a long, lossy path.

**Non-goal:** lowering CPU on either end. The options below are chosen to add negligible CPU.

## 2. Where the bandwidth goes

| Stream | Format today | Rate |
|--------|--------------|------|
| Spectrum | 2048 bins x float32, 15 frames/s | 122 880 B/s = 0.98 Mbps |
| Audio, per VRX | 48 kHz x int16 mono | 96 000 B/s = 0.77 Mbps |

Audio is published per VRX topic and ZeroMQ PUB/SUB filters on the publisher, so only the VRXs a
client subscribes to are sent. Two or three listening VRXs plus the spectrum accounts for ~3 Mbps.
The client may also request a different spectrum `rate_hz`; that has **not** been checked.

Per-message overhead also adds up: each audio frame carries a topic plus a ~150-byte JSON header
(§7 of `messages.md`), so many small frames cost more than the arithmetic above. Frame size is set
by GNU Radio's `work()` chunking (`audio_sink`), not fixed by the protocol.

## 3. Proposals

Ordered by value for effort. P1-P3 are lossless in practice and need no new dependencies.

### P1. 8-bit spectrum (about 4x smaller)

Send `uint8` instead of `float32`. Spectrum levels are dBFS over roughly 140 dB, so 255 steps give
about 0.55 dB resolution, finer than a waterfall can show. One multiply/clip/cast in numpy.

- New spectrum header fields: `"format": "u8"`, `"db_min": -140.0`, `"db_max": 0.0`
  (`value_db = db_min + q * (db_max - db_min) / 255`). Sending the window in the header lets it
  change later without a protocol revision.
- Absent `format` means `f32` (today's behaviour).
- Spectrum: 0.98 -> ~0.25 Mbps.

### P2. Fewer bins and slower frames

The client draws ~1000-1500 pixels, so 2048 bins is more than it uses. `configure_spectrum`
already carries `fft_size` and `rate_hz`. 1024 bins at 10 frames/s (with P1) is ~82 kbps.

- **Trade-off:** the capture is displayed over a 250 kHz span, so halving `fft_size` doubles the
  bin width (about 122 Hz -> 244 Hz) and degrades resolution when the user zooms in. Either keep
  `fft_size` fixed and max-hold decimate to the client's pixel width, or accept the coarser bins
  in the `remote` profile only. Decide when implementing.
- Slower frame rate trades waterfall smoothness for bandwidth; 5-10 fps remains usable.

### P3. 12 kHz audio (about 4x smaller)

The demod passband is 200-3000 Hz (`DEFAULT_FILTERS`), so 48 kHz is four times oversampled. At
12 kHz int16 the audio is ~192 kbps per VRX with Nyquist at 6 kHz: no audible loss for these
modes. WSJT-X uses 12 kHz internally. The client already resamples to the output device's mix
format (NAudio), so it only needs to honour `rate_hz` from the audio header, which the protocol
already carries. Also lowers server work (fewer samples through the audio chain).

- Negotiated through `get_capabilities` (`audio_rate_hz` becomes a list) and a new request field,
  see §4. Wider modes (AM, NFM) need a rate that covers their bandwidth; never go below what the
  VRX filter passes.

### P4. Opus audio (about 30-60x smaller): only if needed

12-32 kbps per VRX against 768 kbps. Costs that make it a last resort:

- **Lossy.** FT8 and other weak-signal modes must be shown not to lose decodes before it is
  offered for them. Test: same audio through Opus at the proposed bitrate and uncompressed, count
  decodes over a few hours of band activity.
- **Dependencies on both ends:** libopus on the server (check radioconda), a managed or native Opus
  library in the C# client.
- **Added latency** (codec frame, typically 10-20 ms) plus jitter handling for lost packets.
- Appears as a new `audio_formats` entry (`"opus"`) in `get_capabilities`, which the protocol
  already lists as a capability set, so no structural change.

### Not recommended

General-purpose compressors (zlib, lz4) on either stream: noisy spectra and audio barely compress,
so the CPU buys almost nothing.

## 4. Negotiation and profiles

Make bandwidth a **profile the client chooses**, not a server-wide setting.

| Profile | Spectrum | Audio (per VRX) | Total, one VRX (computed) |
|---------|----------|-----------------|---------------------------|
| `lan` (today's default) | f32, 2048 bins, 15 fps | 48 kHz int16 | ~1.75 Mbps |
| `remote` | u8, ~1024 bins, 5-10 fps | 12 kHz int16 | ~0.25-0.3 Mbps |
| `remote-opus` (optional) | as `remote` | Opus ~24 kbps | ~0.1 Mbps |

Mechanics (all additive; defaults reproduce today):

1. `get_capabilities` advertises what the server can do:
   `"spectrum_formats": ["f32", "u8"]`, `"audio_rates_hz": [48000, 12000]`,
   `"audio_formats": ["int16", "f32", "opus"]` (existing key).
2. The client selects per stream: `configure_spectrum` gains `"format"`; `add_vrx` (or an
   `update_vrx`) gains `"audio_rate_hz"` and `"audio_format"`. Unknown or unsupported values return
   `bad_request` and leave current settings unchanged.
3. Because audio is per VRX, one client can run a high-quality local VRX and a thin remote one, and
   two clients (GUI and 101Cats) can use different profiles without interfering.
4. The client UI exposes a single "Connection: LAN / Remote" control that sets the bundle; the
   individual knobs stay in the protocol for testing.

## 5. Interaction with other designs

- **Supervisor / twin-PC** ([`server_lifecycle.md`](server_lifecycle.md) §12): unaffected; the
  profile applies after `Connect()`. The supervisor's start parameters stay whitelisted, so a
  profile should **not** be a supervisor `start` option.
- **101Cats integration** ([`integration_design.md`](integration_design.md)): if 101Cats only needs
  control and events, it can subscribe to no audio and no spectrum, and costs nothing here.
- **FT8 and timing:** a remote path adds latency that shifts decode DT. WSJT-X tolerates a limited
  range; verify on a realistic remote link before relying on it.

## 6. Measure before building

Wi-Fi dropouts that affect audio but not the waterfall point at **jitter**, not exhausted
bandwidth: a TCP stall followed by a burst can overflow a small audio buffer even when the average
rate is fine. The audio header already carries `seq`, so the client can distinguish:

- **Gaps** in `seq` (frames actually lost at the publisher: ZeroMQ PUB drops when its send queue
  fills) versus
- **Late arrival** (no gap, but inter-arrival time spikes, so the client's buffer ran dry).

A small client-side counter and log of both would settle whether compression or a deeper jitter
buffer is the right fix. Compression is still worth doing for the remote use case, but this tells
us whether it will cure the Wi-Fi glitches.

## 7. Suggested order

1. Client-side gap/late counters (§6), so there is data.
2. P1 and P3 (lossless, trivial CPU, ~4x each) behind capability negotiation.
3. P2 once a coarser-resolution `remote` profile is acceptable.
4. The client "Connection" profile control.
5. P4 (Opus) only if (2)-(3) are not enough, after the FT8 decode test.

## 8. Open questions

- What `rate_hz` does the client actually request today, and how many VRXs does the owner usually
  run? (Needed to know which stream dominates the real 3 Mbps.)
- Should audio frames be made larger (fewer messages, less header overhead) at the cost of latency?
  Interacts with the jitter buffer depth.
- Should the `remote` profile be a server-side concept (the server enforces a bandwidth cap) or
  purely client-chosen? Leaning client-chosen: simpler and the server stays stateless about links.
- Encryption/auth of a remote link is out of scope here: it is covered by the VPN stance in
  `server_lifecycle.md` §12.5.

## 9. Acceptance criteria

1. With no profile requested, wire output is byte-for-byte compatible with today (old clients
   unaffected).
2. `remote` profile cuts measured throughput for one VRX to under ~0.4 Mbps (measure with a
   packet counter, not the arithmetic above).
3. Waterfall remains usable (signals identifiable and clickable) at the reduced bins/fps.
4. FT8 decodes over `remote` (P1-P3 only) match `lan` for the same band over a comparison window.
5. If P4 is built: decode count with Opus is not materially below uncompressed, or Opus is not
   offered for data modes.
6. Server CPU with the `remote` profile is not higher than with `lan`.
