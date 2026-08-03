# Integration Design Note — 101Cats ↔ HF-SDR over ZeroMQ

**Status:** Draft proposal `v0.2-draft`. Extends [`messages.md`](messages.md) v0.1 (which
stays the current contract). Nothing here is implemented yet.

## 1. Goal & decisions (recap)

Two **separate, closely-integrated** apps — the HF-SDR app and 101Cats — talking over
**ZeroMQ**, so they feel like one operating environment. Key decisions already made:

- **The SDR is the VFO; the 101D follows.** Default sync direction is **rig-follows-SDR**:
  you tune on the SDR, the 101D tracks whatever you're listening to so it's ready to transmit.
- **Linked-VRX model.** At most **one** VRX is the *rig-linked* VRX at a time; all other VRXs
  are free monitors the rig never touches. Linking is explicit and directional — no polling
  race (cf. SDR Uno's global RSYN). "Park on a DX and keep exploring" = **unlink**, not reverse.
- **Link state lives in the server** (single source of truth) — both the GUI and 101Cats
  observe and drive it. Consequence of using ZeroMQ as the bus.
- **Push, not poll.** The server *publishes* linked-VRX changes; 101Cats subscribes and drives
  the real rig. No polling loop anywhere.
- Single operating VFO (rig main ↔ one VRX); dual/sub-RX out of scope for now.

## 2. Transport

101Cats becomes **another ZeroMQ peer of the SDR server**, reusing the existing three sockets:

| Socket    | 101Cats uses it for | Notes |
|-----------|---------------------|-------|
| `control` | sends commands (link, tune, TX state) | request/reply |
| `stream`  | **subscribes** to events (`event` topic) | the push channel — link/TX/tune events |
| `audio`   | *not used* | SDR audio reaches WSJT-X etc. via the Voicemeeter virtual cable, not ZMQ |

### 2.1 Control channel: `REQ/REP` → `ROUTER`/`DEALER`

Today's control channel is `REQ/REP` and assumes a **single** client. With 101Cats as a
*second* controller alongside the GUI, upgrade the server's control socket to **`ROUTER`**
(async, multi-client); new clients connect with **`DEALER`**. The JSON envelope is unchanged
(`{id,cmd,params}` → `{id,ok,result|error}`); `id` still correlates reply to request.

- **Migration is gentle:** a `ROUTER` server still serves existing `REQ` clients, so the GUI
  can stay `REQ` for now and move to `DEALER` later. Unsolicited pushes never go on the control
  socket (they go on `stream`), so `REQ`'s strict alternation isn't a problem.
- This is the `ROUTER/DEALER` item already earmarked in `messages.md` §10.

### 2.2 Echo suppression

Every state-change **event carries an `origin`** tag (the client name that caused it, e.g.
`"gui"`, `"cat"`, or `"server"` for a front-panel-driven or internal change). Each consumer
**ignores events whose `origin` is itself**. This is what prevents the classic
A→B→A CAT feedback loop without any timing hacks. Clients declare their name in `hello`
(already carried as the `client` field).

## 3. Server-owned link state

New fields in the server, surfaced in `get_status`:

```json
"link": {
  "linked_vrx_id": 3,          // null when unlinked
  "direction": "rig_follows_sdr",
  "rig_tx": false              // last TX state reported by 101Cats
}
```

## 4. New control commands

| cmd | params | result / effect |
|-----|--------|-----------------|
| `set_link` | `{ "vrx_id": <int\|null> }` | Designate the rig-linked VRX; `null` unlinks. Returns the `link` object. Emits `link_changed`. |
| `tune_link` | `{ "freq_hz": <int>, "mode": <str> }` (either optional) | Retune the **currently linked VRX** without needing its id — used by 101Cats for the occasional *SDR-follows-rig* case (rig tuned at the front panel). Errors `wrong_mode`/`bad_request` if nothing linked. Emits `linked_tuned` (origin = caller). |
| `set_rig_tx` | `{ "tx": <bool> }` | 101Cats reports the 101D's transmit state; the server **mutes the linked VRX** while `tx=true` (you don't monitor your own transmit). Emits `rig_tx`. |

Notes:
- The GUI keeps using `update_vrx`/`add_vrx` as today. When the *linked* VRX is retuned by any
  path, the server emits `linked_tuned` — that is the rig-follows-SDR push.
- `tune_link` is a convenience so 101Cats never has to track the linked `vrx_id`; it always
  addresses "whatever is linked."

## 5. New events (`stream` socket, `event` topic, header-only)

Extends the §6.2 event shape (`seq`, `t_utc_ms`, `code`, …) with per-code fields:

| code | fields | meaning |
|------|--------|---------|
| `link_changed` | `linked_vrx_id`, `freq_hz`, `mode`, `direction` | The link was set/cleared or the linked VRX changed identity. |
| `linked_tuned` | `vrx_id`, `freq_hz`, `mode`, `origin` | The linked VRX's freq/mode changed. **101Cats drives the rig from this** (ignores its own `origin`). |
| `rig_tx` | `tx`, `origin` | Rig TX state changed (so the GUI can show TX and the server mute). |

## 6. Flows

**Rig-follows-SDR (the default — tune on the SDR):**
```
GUI --update_vrx(linked, f)--> server   (origin: gui)
server: retune VRX; PUB event linked_tuned{f, origin: gui}
101Cats (SUB) <-- linked_tuned{f, gui} : sets FTdx101D to f
   (101Cats ignores nothing here; it did not originate this)
```

**SDR-follows-rig (occasional — front-panel tune):**
```
101Cats detects rig freq change (its existing job)
101Cats --tune_link(f)--> server        (origin: cat)
server: retune linked VRX; PUB linked_tuned{f, origin: cat}
GUI <-- linked_tuned{f, cat} : updates display
101Cats <-- linked_tuned{f, cat} : IGNORES (origin == self) -> no loop
```

**Park on a DX (keep exploring):**
```
101Cats --set_link(null)--> server       (unlink)
server: linked_vrx_id = null; PUB link_changed{null}
=> rig now holds its own freq; all VRXs free. Re-link later with set_link(vrx_id).
```

**TX mute:**
```
101Cats --set_rig_tx(true)--> server  : server mutes linked VRX; PUB rig_tx{true}
101Cats --set_rig_tx(false)--> server : unmute; PUB rig_tx{false}
```

## 7. Out of scope / future

- **Richer property sync** (mode/filter/AGC/squelch two-way, and pushing the SDR's
  `signal_power`/`signal_snr` S-meter to 101Cats) — additive events/commands once the link core
  works. Maps onto the SDRConnect property set for familiarity.
- **SDRConnect WebSocket API compatibility** is a *separate, optional* surface for the broader
  ecosystem (other tools / SDRConnect-compatible clients) — **not** the 101Cats path, which is
  this native ZeroMQ link.
- Dual/sub-RX linking; split-frequency handling beyond simple TX mute.

## 8. Open questions

- **Where does 101Cats's ZMQ client live** inside 101Cats (a small integration service vs. the
  main VM)? — a 101Cats-side decision, deferred until it's healthy again.
- Do we want `tune_link` to also carry **filter/bandwidth**, or keep the first cut to freq+mode
  (matching what the Kenwood path did) and add the rest with the property-sync phase?
- Should **unlink auto-mute** or leave the freed VRXs audible? (Leaning: leave audible — unlink
  is exactly when you want to keep listening.)
