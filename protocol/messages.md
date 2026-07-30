# HF SDR — ZeroMQ Message Contract

**Status:** Draft `v0.1`. The boundary between the GNU Radio **server** (all
RF/DSP) and the Avalonia **client** (UI). Control is JSON now; bulk payloads are
binary. A later revision may migrate the control/header encoding to Protobuf —
the message *shapes* here are designed to map cleanly onto that.

---

## 1. Design principles

- **Server owns all DSP and all state.** The client is a view/controller; it
  never sees raw IQ. The server sends *processed* data only: FFT magnitudes and
  demodulated audio.
- **Control is explicit request/reply; data is fire-and-forget pub/sub.** A
  dropped spectrum or audio frame is never fatal — the next one supersedes it.
- **Diversity and multi-VRX are first-class**, designed in from the start (not
  bolted on).
- **Units:** frequency in **Hz** (integer where possible), levels in **dBFS**
  (spectrum) / **dB** (gains), time in **UTC milliseconds** unless noted.
- **Binary payloads are little-endian** (x86 both ends).
- **The server is authoritative:** every state-changing command returns the
  resulting state, and `get_status` is always the source of truth.

---

## 2. Transport & sockets

| Socket    | ZMQ pattern              | Default port | Direction        | Carries |
|-----------|--------------------------|--------------|------------------|---------|
| `control` | `REQ` (client) / `REP` (server) | 5555 | client → server → client | JSON commands + replies |
| `stream`  | `PUB` (server) / `SUB` (client) | 5556 | server → client | spectrum frames, events, telemetry |
| `audio`   | `PUB` (server) / `SUB` (client) | 5557 | server → client | per-VRX demodulated audio |

Audio is a **separate** PUB socket from spectrum so a slow spectrum/UI consumer
never stalls audio delivery.

### Framing

- **Control** messages are a single frame: one UTF-8 JSON object.
- **`stream` / `audio`** messages are **multipart**:
  1. `topic` — UTF-8 string, used for `SUB` subscription filtering
     (e.g. `spectrum/combined`, `audio/3`, `event`, `telemetry`).
  2. `header` — UTF-8 JSON object (metadata for the payload).
  3. `payload` — binary blob (optional; absent for `event`/`telemetry`).

`REQ/REP` is strictly alternating and assumes a **single client** for `v0.1`.
Multiple/again-async clients (ROUTER/DEALER) is a future revision (§10).

---

## 3. Versioning & handshake

`protocol_version` is `MAJOR.MINOR`. Client sends `hello` first; the server
rejects a mismatched **major**. Minor differences are backward-compatible
(client should ignore unknown fields).

```json
// → request
{ "id": 1, "cmd": "hello", "params": { "protocol_version": "0.1", "client": "HfSdr.App/0.1" } }
// ← reply
{ "id": 1, "ok": true, "result": { "protocol_version": "0.1", "server": "hf-sdr-server/0.1" } }
```

---

## 4. Control channel (`REQ`/`REP`)

### 4.1 Envelope

Every request:
```json
{ "id": <int>, "cmd": "<string>", "params": { ... } }
```
Every reply:
```json
{ "id": <int>, "ok": true,  "result": { ... } }
{ "id": <int>, "ok": false, "error":  { "code": "<string>", "message": "<human text>" } }
```
`id` is an opaque client-chosen correlation value, echoed back.

### 4.2 Session & discovery

| cmd | params | result |
|-----|--------|--------|
| `hello` | `protocol_version`, `client` | `protocol_version`, `server` |
| `get_status` | – | full state snapshot (§4.7) |
| `get_capabilities` | – | supported modes/features (§4.8) |

### 4.3 Tuner & device

| cmd | params | notes |
|-----|--------|-------|
| `set_tuner_mode` | `{ "mode": "single"\|"diversity"\|"independent" }` | **Disruptive** — server tears down and rebuilds the flowgraph; existing VRXs may be dropped. Reply returns the new status. Brief audio gap. |
| `set_center_freq` | `{ "hz": <int>, "tuner": <0\|1> }` | `tuner` required only in `independent` mode; ignored/locked otherwise. |
| `set_sample_rate` | `{ "hz": <int> }` | Capture width. Dual-tuner modes are fixed at 2 000 000. |
| `set_bandwidth` | `{ "hz": <int> }` | IF bandwidth. |
| `set_gain` | `{ "agc": <bool>, "if_gr_db": <int>, "rf_gr_db": <int>, "agc_setpoint_dbfs": <int>, "tuner": <0\|1> }` | Gains are **gain reduction** in dB, given as **positive** values (higher = less gain). Both are clamped to the live valid range (see `*_range` in status). `rf_gr_db` snaps to the nearest discrete **LNA step** for the current band (on HF: `{0,6,12,18,37,42,61}` dB / states 0–6); the reply/status echo the actual applied value + resulting `lna_state`. The full discrete step list is reported as `rf_gr_db_steps` (see status §4.7) so the client can offer one detent per step rather than a coarse continuous slider. `if_gr_db` used only when `agc=false`. `tuner` only meaningful in `independent`. |

### 4.4 VRX (virtual receivers)

A **VRX** is one demodulated receiver within the current capture window.
Multiple VRXs can share a single-tuner capture (the reliable way to run several
in-band receivers).

| cmd | params | result |
|-----|--------|--------|
| `add_vrx` | `{ "freq_hz": <int>, "mode": "<demod>", "filter": {"low_hz":<int>,"high_hz":<int>}, "squelch_dbfs": <int\|null>, "volume": <0..1>, "tuner": <0\|1> }` | `{ "vrx_id": <int> }` |
| `update_vrx` | `{ "vrx_id": <int>, ...any subset of add_vrx fields... }` | updated VRX object |
| `remove_vrx` | `{ "vrx_id": <int> }` | – |
| `list_vrx` | – | `{ "vrx": [ <vrx object>, ... ] }` |

`freq_hz` must fall within the active capture window (± sample_rate/2 of the
tuner centre) or the server returns `out_of_range`. `mode` ∈ demod enum (§5).
`filter` is the audio passband (e.g. `{-2700,-200}` for LSB); omit for a
mode-appropriate default. Audio for the VRX is published on `audio/<vrx_id>`.

### 4.5 Diversity

| cmd | params | notes |
|-----|--------|-------|
| `set_combiner` | `{ "type": "mrc"\|"egc", "auto": <bool>, "phase_deg": <float>, "amp": <float>, "phase_lock": <bool>, "amp_lock": <bool> }` | `diversity` mode only. `auto=true` → adaptive alignment; manual `phase_deg`/`amp` used when locked (mirrors SDRConnect's diversity panel). |
| `null_signal` | `{ "center_hz": <int>, "width_hz": <int>, "track": <bool>, "track_speed": "fast"\|"med"\|"slow", "amp": <float>, "phase_deg": <float>, "clear": <bool> }` | `diversity` mode only (else `wrong_mode`). **Targeted interference canceller.** `center_hz` engages/retargets a null on the source at that frequency; the server band-isolates both branches around it (`width_hz`, default 12 000, clamped 1 000–40 000) and estimates the cancelling weight `w = h0/h1` there, then applies `y = x0 − w·x1` across the whole capture. `track` (default `true`) keeps re-estimating so the null follows drift; `track:false` freezes it. `track_speed` sets the adaptation time constant (default `med`) — use `slow` for a fading skywave source to steady the weight. `amp`/`phase_deg` set the weight manually (fine-trim; switches off tracking). `clear:true` disengages. Reply = the combiner null object below. |

Combiner state is reported in `get_status` (§4.7) under `combiner`: MRC → `{ "type":"mrc", "auto":true, "amp", "phase_deg" }`; null → `{ "type":"null", "active", "track", "manual", "amp", "phase_deg", "null_depth_db", "track_speed", "center_hz", "width_hz" }`. `null_depth_db` is the measured cancellation in the target band. The estimator is fade-robust: during a deep fade of the target on branch B it holds the last weight (rather than dividing into the noise), regularises the denominator, and caps `|w|`. Live correction estimate is also reported via `telemetry` (§6.3).

### 4.6 Spectrum & streaming

| cmd | params | notes |
|-----|--------|-------|
| `configure_spectrum` | `{ "fft_size": <int>, "rate_hz": <float>, "sources": ["combined","a","b","0","1"] }` | `rate_hz` = frames/sec. `sources` selects which spectra to publish (mode-dependent, §7). |
| `start` | `{ "audio": <bool>, "spectrum": <bool> }` | begin publishing selected streams |
| `stop` | `{ "audio": <bool>, "spectrum": <bool> }` | stop publishing (server keeps running) |

### 4.7 `get_status` result (state snapshot)

```json
{
  "protocol_version": "0.1",
  "tuner_mode": "diversity",
  "device": { "name": "RSPduo", "serial": "2305039434" },
  "capture": { "center_hz": 7150000, "sample_rate_hz": 2000000, "bandwidth_hz": 1536000 },
  "gain": { "agc": true, "if_gr_db": 40, "rf_gr_db": 37, "agc_setpoint_dbfs": -30,
            "lna_state": 4, "rf_gr_db_range": [0, 61], "if_gr_db_range": [20, 59],
            "rf_gr_db_steps": [0, 6, 12, 18, 37, 42, 61] },
  "combiner": { "type": "mrc", "auto": true, "amp": 1.31, "phase_deg": 148.4 },
  "vrx": [
    { "vrx_id": 1, "freq_hz": 7150000, "mode": "lsb",
      "filter": { "low_hz": -2400, "high_hz": -300 },
      "squelch_dbfs": null, "volume": 0.5, "tuner": 0 }
  ],
  "streaming": { "audio": true, "spectrum": true }
}
```

### 4.8 `get_capabilities` result

```json
{
  "tuner_modes": ["single", "diversity", "independent"],
  "demod_modes": ["lsb", "usb", "cw", "am", "nfm"],
  "max_vrx": 8,
  "sample_rates_hz": [2000000],
  "audio_rate_hz": 48000,
  "audio_formats": ["int16", "f32"],
  "features": ["diversity", "diversity_null", "multi_vrx", "noise_blanker", "notch"]
}
```

---

## 5. Enumerations

- **`tuner_mode`**: `single` · `diversity` · `independent`
- **`demod` (VRX mode)**: `lsb` · `usb` · `cw` · `am` · `nfm`  *(build order per project priorities)*
- **`spectrum source`**: `0`/`1` (per-tuner in single/independent) · `a`/`b`
  (per-antenna branches in diversity) · `combined` (post-combiner)
- **`audio_format`**: `int16` (default, compact) · `f32`

---

## 6. `stream` channel (server → client, PUB)

### 6.1 `spectrum/<source>`
- **header**
  ```json
  { "seq": 4021, "source": "combined", "center_hz": 7150000, "span_hz": 250000,
    "fft_size": 2048, "ref_dbfs": 0, "peak_dbfs": -12.4, "overload": false,
    "t_utc_ms": 1753500000123 }
  ```
- **payload**: `fft_size` × `float32`, magnitude in **dBFS**, ordered low→high
  frequency (already `fftshift`-ed), spanning `center_hz ± span_hz/2`.
- `peak_dbfs`: highest raw-stream sample peak across tuners (fc32 full scale =
  ADC full scale = 0 dBFS), i.e. live ADC headroom. `overload`: true when
  `peak_dbfs` reaches the server's overload threshold (~-1 dBFS) — surfaced
  because gr-sdrplay3's own overload warning is log-only (no message port).

### 6.2 `event`
Async server notifications; **no payload frame**, header only:
```json
{ "seq": 88, "level": "warning", "code": "adc_overload",
  "message": "ADC overload corrected", "t_utc_ms": 1753500000200 }
```
Codes: `adc_overload`, `gain_changed`, `tuner_mode_changed`, `vrx_dropped`,
`device_error`.

### 6.3 `telemetry`
Periodic metrics, header only:
```json
{ "seq": 300, "t_utc_ms": 1753500000300,
  "branch_dbfs": { "a": -33.2, "b": -30.1, "combined": -26.6 },
  "combiner": { "amp": 1.31, "phase_deg": 148.4 } }
```

---

## 7. `audio` channel (server → client, PUB)

### `audio/<vrx_id>`
- **header**
  ```json
  { "vrx_id": 1, "seq": 100234, "rate_hz": 48000, "format": "int16",
    "channels": 1, "samples": 1024, "t_utc_ms": 1753500000150 }
  ```
- **payload**: `samples` × `channels` interleaved samples in `format`
  (`int16` = signed little-endian; `f32` = IEEE-754 little-endian).

One topic per VRX; the client subscribes to the VRXs it is playing.

---

## 8. Capture windows by tuner mode (§ concept)

- **single** — one capture (`0`); VRXs must lie within `center ± rate/2`.
  Multiple in-band VRXs run here (the reliable multi-receiver path).
- **diversity** — one *combined* capture from two coherent antennas; VRXs run on
  the combined stream. Spectra available: `a`, `b`, `combined`.
- **independent** — two separate captures (`0`, `1`) at different `center_hz`;
  each VRX names its `tuner`. (Server-side dual-tuner init is currently
  unreliable — see docs/SETUP_NOTES.md; the contract is unaffected.)

---

## 9. Error model

`error.code` values: `bad_request` · `unknown_cmd` · `unsupported` ·
`out_of_range` · `wrong_mode` · `busy` · `device_error` · `version_mismatch`.
Errors never crash the session; the client may retry or re-`get_status`.

---

## 10. Future revisions

- Protobuf encoding for control + headers (shapes above map directly).
- Multiple/async clients via `ROUTER`/`DEALER` (drop the REQ/REP alternation).
- Auth/TLS (`CURVE`) if exposed beyond localhost.
- IQ recording / playback control; frequency memories; scanning.
