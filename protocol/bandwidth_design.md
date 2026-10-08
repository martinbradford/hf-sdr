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

**Implemented (not yet exercised against a live server).** `client/HfSdr.App/AudioStats.cs`, shown
as a readout beside the peak meter (tooltip has detail), plus a log at
`%APPDATA%\HfSdr\audio-events.log` that gets a line for each second in which anything new happened:

| Counter | Meaning | Points at |
|---------|---------|-----------|
| **lost** | frames missing from the per-VRX `seq` | frame never arrived: server queue or ZeroMQ drop |
| **late** | gap between frames > 100 ms (no loss) | Wi-Fi/TCP stall then burst: needs a deeper buffer |
| **dry** | playback read padded with at least 2 ms of silence (count, ms) | what you actually *hear*; follows from late or lost |
| **resync** | backlog > 400 ms discarded (count, ms) | the existing catch-up clear; itself an audible jump |

Server side, `get_status` reports `streaming.dropped_frames` (frames dropped in the server's own
send queue). Reading the two together: lost > 0 with `dropped_frames` == 0 means the loss happened
after the server queue (network or subscriber); lost > 0 matching `dropped_frames` means the server
could not keep up. Caveats: a new VRX or a seq restart is not counted as loss or lateness; and the
100 ms stall threshold is a first guess, to be tuned once real numbers exist.

**First colocated run (2026-10-08) and what changed.** With the link clean (lost 0, late 0, resync
0, longest inter-frame gap 45-52 ms) the readout still showed `dry 16`. The event log showed ~155 ms
of that was the start-up moment (playback began before any audio was buffered) and most of the rest
were shortfalls of a fraction of a millisecond (a few samples); one 22 ms event was the only
possibly-audible gap. Buffered audio at those moments was 0-63 ms: playback ran nearly empty. So:

- **Metric fixed.** A dry read is counted only if the silence is at least 2 ms *and* audio resumes
  within 1 s (so the end of a stream is not a "glitch"); smaller shortfalls are tracked separately as
  `micro` (tooltip only), and reads while the cushion is filling are not counted at all.
- **Playback cushion added** (`PrimedWaveProvider`): playback is held until `AudioPrimeMs` (default
  80, client `settings.json`, 0-300, 0 = off) of audio is buffered, and re-primes after a real dry
  spell. It costs that much extra latency and absorbs jitter up to that size. Tune it from the log:
  if `dry` still appears over Wi-Fi, raise it; if the extra latency bothers you (FT8 DT), lower it.

Note the existing `resync` is not a metric only: clearing up to 400 ms of audio to catch up is an
audible glitch in its own right, and may be responsible for some of the reported dropouts after a
Wi-Fi stall. The counters will show whether it is. (In the Wi-Fi runs below `resync` stayed 0.)

### 6.1 Wi-Fi results (2026-10-08, laptop client over Wi-Fi to the shack PC via the supervisor)

One run per cushion setting; durations are approximate and the runs were **not** tightly
controlled (no deliberate load, laptop near-idle with a little web browsing), so treat the figures
as indicative. Audio sounded smooth in all but was only listened to informally.

| `AudioPrimeMs` | Laptop attached to | Duration | lost | late | dry reads | silence | longest gap | buffered level |
|----------------|--------------------|----------|------|------|-----------|---------|-------------|----------------|
| 0 | Living Room node | ~90 s | 0 | 10 | 8 (4 at connect) | 46 ms | 285 ms | 119-256 ms after stalls |
| 80 | Living Room node | ~13.5 min | 0 | 27 | 6 (5 in the first ~15 s) | 48 ms | 255 ms | ~200-260 ms |
| 250 | Living Room node | ~10 min | 0 | 49 | **0** | 0 ms | 274 ms | ~225-270 ms |
| 250 | Office AP (mesh master), mesh roaming off | ~9 min | 0 | 12 | 1 (9 ms, at the very start) | 9 ms | 314 ms | ~255-310 ms |
| 250 | **none: client on the shack PC itself, `localhost`** | **> 15 min** (exact length unknown: no log is written when nothing happens) | 0 | **0** | **0** | 0 ms | not recorded (no `late`, so under 100 ms) | n/a |

`resync` was 0 in every run, including the colocated one.

What this shows:

- **Nothing is lost; the path delivers late.** `lost` is 0 and the stalls are 100-314 ms bursts, so
  this is jitter, not bandwidth. Compressing the streams would not have helped this symptom.
- **The buffer ratchets up on its own.** After a stall the burst delivers all the delayed audio but
  playback is real-time, so the delay stays as extra latency (the buffer climbs to about the longest
  stall seen and stays there). Even with the cushion at 0 or 80, the buffer settled near 250 ms
  within minutes, which then protected against repeats; the dry reads occurred while it was still
  climbing.
- **250 ms removes the glitches** (dry 0 over ~10 min, despite 49 late events and stalls up to
  274 ms) and costs about the same steady-state latency the other settings drift to anyway, but
  ~170 ms more than 80 does at the start of a session.
- A 100 ms "late" threshold is, by construction, longer than an 80 ms cushion can cover, so with
  that setting dry reads are expected until the buffer has ratcheted up.
- **Stalls are not periodic** across runs (an early guess of ~5-6 minute spacing did not hold).

**Recommendation:** `AudioPrimeMs` = 250 for Wi-Fi clients. Keep the default of 80 for colocated
use: a >15 minute colocated run showed no stall at all (`late 0`; that run used 250, but the `late`
counter does not depend on the cushion), so a small cushion is enough there. A wired client on the
LAN has **not** been measured over a long run, so 80 is provisional for that case. Possible later
improvement: a cushion that adapts to the link, or a UI selector.

**Where the stalls come from: the network path between the two PCs; not the sender; exact hop not
yet determined.**

Known:
- The Windows WLAN report for the laptop shows an excellent link (5 GHz channel 40, 802.11ax,
  -39 dBm, 1201 Mbps) and **no disconnects, roams or reassociations** during the tests (it does not
  record background scans, airtime contention or AP-side steering).
- Topology: the laptop was on the Living Room mesh node, which reaches the master (in the shack) by
  **wireless backhaul**; the shack PC is wired (1G) to a switch to the pfSense router, and the
  master AP is wired to the same switch.
- **Moving the laptop to the mesh master (wired uplink) with the control panel's per-device "Mesh
  Technology" off, which stops it roaming, helped only modestly:** about 12 late events in ~9 min
  versus 49 in ~10 min. But stall rates across earlier runs already ranged from roughly 2 to 6.7 per
  minute (this run: ~1.3), so the difference is not clearly outside run-to-run variation, and the
  longest stall seen so far (314 ms) was in this run, as its first event. Two things were changed at
  once (which node, and roaming), and the 314 ms first event may be server-side (adding a receiver
  locks the flowgraph briefly), not network. So the wireless backhaul and roaming are **not** the
  main cause.

- **The sender is cleared.** An earlier version of this note said the shack-PC side was clean from a
  colocated run lasting only ~20-40 s, which could easily have missed stalls arriving every minute or
  two; that claim was withdrawn. It has now been tested properly: a client on the shack PC itself,
  connected to `localhost`, ran for more than 15 minutes with `lost 0 / late 0 / dry 0 / resync 0`
  (and no log written). The server's audio output therefore stays regular (no gap over 100 ms) under
  the same load, so the 12-49 late events per ~10 minutes seen from the laptop are introduced between
  the two machines.
- **Not settled:** which hop. Loopback says nothing about the shack PC's own network card or the wired
  leg to the switch, though the Wi-Fi hop is the obvious candidate and moving to the master AP
  reduced (but did not remove) the stalls.

Untried tests, in the order I would do them:

1. **A wired client elsewhere in the house against the shack PC for ~15 minutes** (`AudioPrimeMs` 250,
   no retuning). Wired to wired through the same switch tests everything except Wi-Fi. Zero `late`
   points at Wi-Fi (then the laptop's adapter power-management and driver settings, or the mesh
   configuration); stalls still appearing point at the switch, pfSense or the shack PC's NIC.
2. Run `ping -t` with timestamps from the laptop to the router and to the shack PC at the same time
   and compare spikes against the `late` timestamps in `audio-events.log` (both spike = laptop/mesh
   leg; only the shack PC = beyond the master).
3. Laptop on a wired connection, if possible.
4. Check whether the shack PC is on the same subnet as the laptop (192.168.4.x). If it is routed
   through pfSense, anything inspecting traffic there could add jitter.

**Limitation of the log.** `audio-events.log` is written only when something happens, so a clean run
leaves no file and its length cannot be recovered afterwards. A "session start/end" line (time,
host, `AudioPrimeMs`, final counters) would fix that; not implemented.

## 7. Suggested order

1. ~~Client-side gap/late counters (§6), so there is data.~~ Done (needs a live run to produce data).
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
