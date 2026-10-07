# Server Lifecycle Note — launching the server from the client

**Status:** Draft proposal `v0.1-draft`. Extends [`messages.md`](messages.md) v0.1 (which
stays the current contract). Nothing here is implemented yet. Code references are as of
commit `e2bc92c`.

## 1. Goal

Make the SDR feel like **one application**: the user launches the client, the receiver comes
up, and they never see a console, a Python command line, or a second window. Today the server
is started by hand ([`RunServer.ps1`](../RunServer.ps1)) and the client's Connect button
assumes it is already listening. That is fine for development and unacceptable for release.

**Decision: the client spawns and supervises the server as a child process.** It does *not*
host the GNU Radio flowgraph in-process.

## 2. Rejected alternatives

### 2.1 Embedding the flowgraph in-process (CPython hosted in .NET)

Hosting CPython inside the .NET client (Python.NET or a custom host) was considered and
rejected. The objections are concrete, not stylistic:

- **Native extension modules.** GNU Radio's Python bindings are C++ extension modules built
  against radioconda's specific interpreter, with a large DLL dependency graph. Embedding means
  loading *that* `python3xx.dll` plus reproducing the conda environment's DLL search paths
  inside our process. Any mismatch is a load-time crash with no useful diagnostic.
- **Shared-fate crashes.** A segfault anywhere in the flowgraph or the SDRPlay API currently
  kills a restartable server process. Embedded, it takes the GUI down with it — and with it any
  chance of showing the user what happened.
- **GIL vs. the UI thread.** The flowgraph's Python callbacks and Avalonia's dispatcher would
  contend for one process. Avoidable, but it is permanent complexity in exchange for nothing
  the user can see.
- **It breaks the deinit dance.** Gotcha #3 in [`CLAUDE.md`](../CLAUDE.md): clean dual-tuner
  re-init depends on `gc.collect()` driving the gr-sdrplay3 source destructor at the right
  moment. That is delicate in a plain interpreter; in a hosted one, with .NET holding
  references across the boundary, collection timing is no longer ours to reason about. The
  failure mode is a wedged RSP, which is the single worst bug in this project.
- **The payoff is illusory.** 101Cats is to become a *second* ZMQ peer on the control channel
  ([`integration_design.md`](integration_design.md)). If the server lives inside the client,
  the client is hosting a server for another application regardless — all the complexity is
  retained and the architectural boundary is lost. [`CLAUDE.md`](../CLAUDE.md) calls the
  server/ZMQ split the real architecture and the client a thin, replaceable view; embedding
  inverts that.

Precedent: **SDRConnect itself is a separate server process with a client front end** — the
product we are replacing already demonstrates that the unified feel is a packaging and
lifecycle problem, not a process-count problem.

### 2.2 GNU Radio ControlPort / Thrift RPC

GNU Radio does ship **ControlPort**, an RPC layer with Apache Thrift as its backend, and the
hope was that C# could drive the flowgraph through it directly with no Python in the mix. It
does not work, for three independent reasons — any one of them sufficient.

**(a) The component is enabled here; the transport is not.** This is the likely source of the
claim: `gnuradio-config-info --enabled-components` *does* list `gr-ctrlport`. That is the
registration machinery, not a working RPC server. Verified on this machine (GNU Radio 3.10.12,
radioconda, `gnuradio-sdrplay3` 3.11.0.8):

| Check | Result |
|-------|--------|
| `gr-ctrlport` in enabled components | present |
| `from gnuradio import ctrlport` | imports |
| `import thrift` (Python package) | **ModuleNotFoundError** |
| `gnuradio.ctrlport.GNURadio` (generated Thrift stubs) | **ImportError** |
| any `*.thrift` IDL or `thrift*.dll` on disk | **nothing** |
| `[ControlPort] on =` in `gnuradio-runtime.conf` | `False` |

Obtaining a transport means rebuilding GNU Radio from source with Thrift enabled, on Windows,
inside a conda environment — and `gnuradio-sdrplay3` is a **prebuilt binary** from fventuri's
releases pinned to this ABI, so it likely has to be rebuilt too. A large and permanent
maintenance burden before any C# is written.

**(b) ControlPort cannot configure a flowgraph — the fatal objection.** It is a knob and
telemetry interface onto an **already-constructed, already-running** flowgraph; the Thrift
service is essentially `getKnobs` / `setKnobs` / `properties`. There is no API to create a
block, connect blocks, or alter topology. Nearly everything this server does is construction or
server-side logic, not parameter setting:

| Operation | What it actually is |
|-----------|---------------------|
| `add_vrx` / `remove_vrx` | builds and connects a demod chain — topology |
| `set_tuner_mode` | tears down and rebuilds the whole flowgraph |
| `null_signal` | installs a canceller and computes its weights |
| `configure_spectrum` | reconfigures the FFT path |
| `set_gain` | sign negation + LNA-step clamping (gotchas #2, #5) |

And something must still *build and run* the flowgraph. ControlPort does not remove Python — it
adds a second control channel into the running Python process. The premise does not hold.

**(c) gr-sdrplay3 registers no knobs anyway.** Blocks appear on ControlPort only if they
register them in `setup_rpc()`. Checking which libraries import the registration API:

| Library | imports `rpcbasic_register_set`? |
|---------|--------------------------------|
| `gnuradio-blocks.dll` | yes — registers knobs |
| `gnuradio-analog.dll` | yes — registers knobs |
| **`gnuradio-sdrplay3.dll`** | **no** — none of the registration API |

The in-tree libraries importing it confirms the check is sound; gr-sdrplay3 inherits the empty
default from `basic_block`. So even a working Thrift server would expose a flowgraph with **no
way to tune it** — not even frequency or gain.

**Conclusion: ControlPort/Thrift is a dead end for this project.** Not "needs work" — wrong tool.

### 2.3 Rewriting the server in C++ — deferred, not rejected

If the goal is genuinely *no Python in the shipped product*, the honest route is a C++ flowgraph:
GNU Radio **is** a C++ library, and gr-sdrplay3 ships `gnuradio-sdrplay3.lib` plus headers, so
the C++ API is available. That yields a native server executable and would genuinely solve the
packaging blocker in §7 — the real obstacle to a public release.

The cost is why it is deferred rather than adopted: it is a rewrite of the whole server,
including the diversity combiner and the fade-robust null canceller — the hardest-won,
hardware-validated DSP in the project — plus a C++ toolchain and GNU Radio dev headers on
Windows. And it still leaves a separate process unless the client P/Invokes into it, which
reintroduces §2.1's shared-fate crash problem in C++ instead of Python.

Revisit it as a deliberate port of a stable, feature-complete server, driven by packaging —
never as a way to avoid spawning a child process.

## 3. Architecture

```
HfSdr.App (Avalonia, one window, one icon)
  +-- Job Object (KILL_ON_JOB_CLOSE)
        +-- python.exe server.py --center ... --control-port ...   [hidden, no console]
              stdout --> readiness banner, then log sink
              stderr --> log sink, surfaced on failure
              ZMQ 5555/5556/5557 --> the existing client sockets, unchanged
```

The wire protocol does not change except for the one addition in §4.1. A hand-started server
remains fully supported (§6).

## 4. Prerequisites

**Implementation status:** 4.1 and 4.2 are implemented in `server.py` (`--shutdown-token`,
`--bind`, bind-before-capture) and covered by `server/python/headless/test_lifecycle.py`
(no hardware; stubs GNU Radio if absent). **Not yet validated on real hardware** — in
particular that `shutdown` leaves the RSP cleanly re-openable (acceptance criterion 4).
4.3 and 4.4 are not started.

Items 4.1–4.4 are **blockers**, not polish. 4.1 and 4.2 are server-side; 4.3 and 4.4 are
client-side.

### 4.1 A `shutdown` control command — the hard blocker

There is no clean way for a parent process to stop the server today. `control_loop`'s `handle`
(`server.py` ~line 665) has no shutdown verb, and the only exit path is SIGINT via the handler
installed in `main()` (`server.py` ~line 732). You cannot deliver SIGINT to a child on Windows
without attaching to its console group — which defeats the point of a hidden child.

The alternative, `Process.Kill()`, is **unacceptable**: it skips `srv.stop(); srv.wait()` and so
skips the SDRPlay device deinit, which is precisely what leaves the RSP wedged.

Proposed addition to §4.2 of the contract:

| cmd | params | result |
|-----|--------|--------|
| `shutdown` | `{ "token": <str> }` (see below) | `{ "stopping": true }`, sent **before** the server begins tearing down. Idempotent. |

Server side is small: reply first, then `stop_evt.set()`. `main()`'s existing wait loop unwinds
through `srv.stop(); srv.wait()`, so the device deinit path is the one already proven by Ctrl-C.

**Authorisation.** Once 101Cats is a second control peer, *any* peer could otherwise kill the
receiver out from under the GUI. Gate it: the client generates a GUID per launch and passes
`--shutdown-token <guid>`; `shutdown` without a matching `token` returns `bad_request`. A server
started by hand gets no token and so cannot be shut down remotely at all — which is the
behaviour you want for a shared or LAN-exposed server. (Alternative considered: honour
`shutdown` only from loopback. Weaker — it does not distinguish the owning client from any other
local peer, and 101Cats is local.)

### 4.2 Bind the control socket before starting capture

In `main()` (`server.py` ~lines 726–737) the order is `srv.start()` then `ctrl.start()`, and
`control_loop` performs its `sock.bind()` *inside* the thread. If the control port is already
taken — a server left running from `RunServer.ps1`, say — the bind raises, that daemon thread
dies, and the main loop continues happily.

The result is a **zombie server: capture and spectrum streaming, no control channel.** Run by
hand you would notice the traceback. Auto-spawned and hidden, the client sees a successful
process start followed by an inexplicable `no reply to 'hello'`.

Fix: bind all three sockets before `srv.start()`, and exit non-zero with a clear message if any
bind fails. This also gives the client a fast, unambiguous "something is already there" signal.
*(Read from the code; not reproduced on hardware.)*

### 4.3 Probe with a throwaway socket

`SdrClient.Send` ([`SdrClient.cs:62`](../client/HfSdr.App/SdrClient.cs:62)) returns cleanly on
timeout, but a ZeroMQ `REQ` socket enforces strict send/receive alternation: after a timed-out
request the socket is wedged and the next send throws. So "try `hello`, spawn if nobody answers,
try `hello` again" **cannot reuse `_control`**.

Either use a fresh socket per probe and only keep the one that succeeds, or adopt the
lazy-pirate pattern (close and recreate on timeout) in `Send` itself. The latter is worth doing
regardless of this feature — today a single timed-out command poisons the control channel for
the rest of the session. The `ROUTER`/`DEALER` upgrade in `integration_design.md` §2.1 also
removes the alternation constraint, so these two pieces of work should be sequenced together.

### 4.4 Reap the child if the client dies

A surviving Python child holds the RSP, so the *next* launch fails at device init. Relying on
the client's shutdown path is not enough — it must survive the client crashing or being killed.

Use a Windows **Job Object** with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`: the child is terminated
by the OS when the parent's handle closes, however the parent exits. Note this is a hard kill,
so it is the *backstop*, not the normal path — the normal path is §4.1, which deinits the
device properly.

## 5. Launch sequence

Readiness needs no new mechanism: the server already prints
`hf-sdr-server: control :5555  stream :5556  audio :5557  centre 7.150 MHz` with `flush=True`
(`server.py` ~line 735). Waiting for that line is deterministic; polling is not needed.

```
client start
  1. probe hello on control port (throwaway socket, ~300 ms)
       ok       --> attach to existing server (§6), done
       no reply --> continue
  2. locate interpreter (§7); if missing -> settings prompt, stop
  3. create Job Object, spawn python.exe server.py (hidden, stdout/stderr piped)
  4. UI: "Starting receiver..." (RSP init is seconds, not milliseconds)
  5. read stdout until the banner line, or
       child exits --> show its last stderr lines, offer Retry
       20 s elapse --> treat as failed, same path
  6. Connect() as today; subscribe; UI live
```

**Failure messages the user will actually hit**, all of which arrive on the child's stderr and
should be surfaced verbatim rather than as a generic connect failure: SDRPlay API service not
running; device claimed by SDRuno/SDRConnect; `gr-sdrplay3 not found` (`server.py` ~line 40);
no device attached.

## 6. Modes

Keep attach-to-existing as a first-class mode — do not replace it.

| Mode | Behaviour | For |
|------|-----------|-----|
| `auto` (default) | Probe; attach if answered, else spawn | Normal use |
| `spawn` | Always spawn; fail if the port is taken | Clean-room testing |
| `attach` | Never spawn; connect to `host:port` | Remote/LAN server, and debugging the server with its own console |
| `supervisor` | Ask a remote supervisor service to start the server, then attach (§12) | Twin-PC: client and RSP on different machines |

`attach` matters more than it looks during development: `dotnet run` with a hand-started server
must not race a spawned one. That is also why §4.3 is a prerequisite rather than a nicety.

With `auto` working, the Connect button becomes a no-op and can eventually be removed, which is
most of the perceived "one app" win.

## 7. Interpreter discovery & packaging

The hardcoded `C:\Users\MABY\radioconda\python.exe` cannot ship. Resolution order:

1. Explicit setting (persisted; also the escape hatch when discovery is wrong).
2. `%USERPROFILE%\radioconda\python.exe`, then common install roots.
3. A `conda env list` / registry probe for a radioconda installation.
4. Fail with a settings prompt naming exactly what is missing — never a silent failure.

Validate the candidate before spawning (`python -c "import gnuradio.sdrplay3"`) so a wrong
interpreter is reported as such rather than as a server crash.

**Packaging, for release:** a radioconda dependency is not a shippable install story for the
general public. Options, in increasing order of effort: document the radioconda prerequisite
(acceptable only for the author's own use); ship a trimmed conda environment alongside the
client; or freeze the server with PyInstaller into a single exe the client launches. PyInstaller
plus GNU Radio's native modules is known-fiddly and wants its own spike. **This section is the
real release blocker, not §4** — §4 is a day's work, this is not.

## 8. UX states

| State | UI |
|-------|-----|
| Locating/validating interpreter | brief, usually invisible |
| Starting receiver | progress text + disabled controls; must not look frozen |
| Live | as today |
| Server died unexpectedly | banner with last stderr lines, Retry button, UI back to disconnected |
| Shutting down | brief; blocks window close until `shutdown` is acknowledged or ~3 s elapse |

The window-close path must send `shutdown` and *wait briefly* for it, or the Job Object's hard
kill (§4.4) takes over and skips the device deinit. `OnClosed` in
[`MainWindow.axaml.cs`](../client/HfSdr.App/MainWindow.axaml.cs) is the hook.

## 9. Interaction with the 101Cats integration

- 101Cats always **attaches**; it never spawns. The GUI owns the server's lifetime.
- Consequence: if the GUI exits while 101Cats is connected, the receiver goes away. Acceptable
  for now (the SDR is the VFO; 101Cats follows it), but if a headless/service deployment is ever
  wanted, the owner role has to move out of the GUI. Worth not designing against.
- The shutdown token (§4.1) is what stops 101Cats from being able to stop the receiver.

## 10. Acceptance criteria

1. Cold start with no server running: client comes up live, no console window appears.
2. Start with a hand-started server already running: client attaches, does not spawn a second.
3. Control port occupied by something else: clear message, no zombie server (§4.2).
4. Normal client exit: server exits cleanly; **immediately relaunching succeeds** (proves the
   device was deinited, not just killed).
5. Client killed from Task Manager: no surviving `python.exe`; next launch succeeds.
6. SDRPlay service stopped: the actual reason is shown in the UI.
7. Single↔diversity switching still works after an auto-spawned start (gotcha #3 regression).

Criteria 4 and 5 are the ones that catch the wedged-device class of bug; they need hardware.

## 11. Out of scope / open questions

- **Multiple server instances / device selection** (more than one RSP): out of scope.
- ~~**Run the server as a Windows service** for a headless shack box~~ — resolved in §12:
  the *server* is not a service (that would hold the RSP permanently); a tiny *supervisor*
  service starts and stops it on demand.
- Should the client **restart** a server that died unexpectedly, or just report it? Leaning
  report-and-offer-Retry: silent restarts hide device problems, and an auto-restart loop against
  a wedged RSP is worse than a stopped app.
- Supervisor open questions are listed in §12.8.
- Does the spawned server want a **log file** (alongside piped stdout) so a user can send a
  diagnostic after the GUI has gone? Probably yes, once there are users.
- Should `--center` and tuner mode at spawn come from **persisted client settings** rather than
  the current hardcoded default? Ties into the persistence work already on the roadmap.

## 12. Remote launch: the supervisor service (twin-PC)

**Status:** supervisor service implemented in `supervisor/` (offline-tested against a fake server; not yet run against the real server/RSP or installed as a service). Builds on §4.1 and §4.2, which remain blockers.

### 12.1 Problem

§§3–8 assume the client and the RSP are on the same PC, so the client can spawn the server as a
child. In a twin-PC deployment the server must run on the PC with the RSP attached, and the
client on the other PC cannot spawn a process there. Starting the server by hand from a terminal
on the shack PC is the inconvenience being removed.

**Constraints (from the owner):**
- The server must **not** run permanently or as a service itself: it claims the RSP exclusively
  and would make it unavailable to SDRConnect/SDRuno and other applications.
- The shack PC does not auto-login and should not need to.
- Single user, home LAN. Remote access from outside the house goes via the owner's existing
  **VPN** (road-warrior), never by exposing ports to the internet.

### 12.2 Decision

A tiny always-on **supervisor** Windows service whose only job is to start, stop and report on
the Python server. It idles at a few MB and never touches the RSP, so the device is claimed only
while the server child is alive.

- **Implementation:** .NET Worker Service (`UseWindowsService()`), NetMQ for the control socket
  (the client already uses NetMQ). Lives in `supervisor/` beside `server/` and `client/`.
- **Owns the child:** it spawns `python.exe server.py` hidden, in a Job Object
  (`KILL_ON_JOB_CLOSE`, §4.4) so a crash of the supervisor cannot leave a process holding the RSP.
- **Reuses the server contract:** readiness is the existing stdout banner (§5); stop is the
  `shutdown` command (§4.1). No new server protocol beyond what §4 already requires.
- **Service, not tray app / auto-login.** A tray launcher would need auto-login (a stored
  password and an unlocked desktop after every reboot) and is no less code. A service survives
  reboots with nobody logged in, which is the point of a shack box you reach remotely.

### 12.3 Supervisor protocol

Separate REQ/REP endpoint, default port **5554** (the server keeps 5555–5557). JSON, same
envelope conventions as [`messages.md`](messages.md), plus a reserved optional `auth` field
(§12.5). Every command is **idempotent**.

| cmd | params | result |
|-----|--------|--------|
| `status` | none | `{ state, pid?, uptime_s?, server_version?, ports?, last_error?, log_tail[] }` |
| `start` | `{ center_hz?, tuner_mode? }` (whitelisted, §12.5) | `{ state }`, returned **immediately**, not when ready |
| `stop` | none | `{ state }`, returned immediately; client polls `status` |

`restart` is deliberately omitted for now (`stop` then `start`). `log_tail` is the last ~20 lines
of the child's stdout/stderr, so the real failure reason reaches the client UI (§5): SDRPlay
service down, device claimed by another app, `gr-sdrplay3 not found`.

**States:** `stopped` → `starting` → `running` → `stopping` → `stopped`, plus `failed`
(child exited unexpectedly or never printed the banner within the 20 s timeout; `last_error` and
`log_tail` populated; cleared by the next `start`).

- `start` while `starting`/`running`: no-op, returns current state.
- `start` is accepted from `stopped` or `failed` only.
- `stop` while `stopped`: no-op.
- `running` means the **banner has been seen**, not merely that the process exists.
- The supervisor never auto-restarts a failed server (same reasoning as §11: silent restart loops
  against a wedged RSP are worse than a stopped app).

### 12.4 Stop semantics: graceful only

`Process.Kill()` skips `srv.stop(); srv.wait()` and so skips the SDRPlay device deinit, which is
what wedges the RSP (§4.1). Therefore:

1. Supervisor sends the server's `shutdown` command on loopback. The supervisor generated and
   holds the `--shutdown-token`, so **the token never crosses the network** and no other peer
   (101Cats included) can stop the receiver.
2. Wait up to ~5 s for process exit.
3. Only then terminate the process, and log that the device may need a power-cycle.

The service's `OnStopping` handler (OS shutdown, `sc stop`) runs the same sequence. The default
Windows service stop budget is finite, but the 5 s graceful window fits inside it.

### 12.5 Security

Deployment is home LAN plus VPN. A "start a process" port on a LAN is still worth constraining.

- **No arbitrary execution.** The interpreter path and server script come from the supervisor's
  own config file (`supervisor.json`), never from a message. `start` accepts only whitelisted,
  validated values (`center_hz` within 0–30 MHz, `tuner_mode` in {single, diversity}). Anything
  else returns `bad_request`. The worst a rogue LAN device can do is start or stop the receiver.
- **Firewall scope.** Inbound rules for 5554–5557 scoped to the home subnet and the VPN subnet.
  Never port-forwarded. The server's own sockets (5555–5557) bind **all interfaces** by default
  (`tcp://*`), so a hand-started server is already LAN-reachable and deserves the same firewall
  scoping. `server.py --bind <addr>` now selects the interface (`127.0.0.1` for local-only); the
  supervisor should pass an explicit value from its config.
- **No shared secret in v1.** Given the VPN/LAN stance it is deferred. The reserved `auth` field
  means a shared secret (or ZMQ CURVE) can be added later without a protocol break; a supervisor
  that has a secret configured rejects messages without it.

### 12.6 Service account

Run as **LocalSystem first**, so no stored password. SYSTEM can normally read the radioconda
install and the headless server needs no desktop, sound device or Qt. Verify on the shack PC:

- radioconda is readable by SYSTEM, and `import gnuradio.sdrplay3` succeeds under it;
- no per-user state is needed (e.g. `~/.gnuradio` prefs, user-scoped conda env vars);
- the SDRPlay API service accepts a session-0 client (it is a system service, so it should).

If any fails, switch the service to log on as the owner's account (one-line change). A
Microsoft-account login would need that account's password for the service logon; a small
local account is less awkward.

### 12.7 Client launch sequence (`supervisor` mode)

Replaces steps 1–5 of §5 when the configured mode is `supervisor`:

```
  1. status on supervisor (throwaway socket, §4.3)
       unreachable -> "Shack PC supervisor not reachable" (service down / PC off / VPN)
       running     -> go to 4
       stopped / failed -> continue
  2. start { center_hz, tuner_mode }
  3. poll status ~every 500 ms:  starting -> show progress
       running -> continue;  failed -> show last_error + log_tail, offer Retry;  20 s -> failed
  4. Connect() to the server at host:5555-5557 (attach, as §6)
```

On client exit the GUI **does not** send `stop` automatically in `supervisor` mode unless the
user opted in ("release the RSP when I disconnect"): the remote machine may be serving another
client, and the user may want the receiver left running. A "Stop receiver" action is always
available. This differs from local `spawn` mode, where closing the GUI stops the server (§8).

### 12.8 Open questions

- Install story: `New-Service` script vs. an installer; startup type Automatic (Delayed) is
  probably right.
- Should the supervisor also expose a **device-busy hint** (is SDRConnect/SDRuno running?) so the
  client can explain a start failure before it happens? Nice-to-have; `log_tail` covers the
  failure after the fact.
- Discovery: a configured host name is enough for a single shack PC; mDNS only if it grows.
- Does local single-PC use also go through the supervisor for one code path, or keep direct
  `spawn`? Leaning keep `spawn`: it requires no service install for anyone without a twin-PC setup.
- Log retention: supervisor writing the child's output to a rotating file under
  `%ProgramData%\hf-sdr\` is cheap and useful once the GUI isn't there to show it.

### 12.9 Acceptance criteria

1. Shack PC freshly rebooted, nobody logged in: client in `supervisor` mode reaches `status`.
2. `start` then `running` then client attaches; RSP is claimed. `stop` releases the RSP and
   SDRConnect can open it immediately.
3. `stop` then `start` repeatedly (10 or more times) with no wedged device (graceful path, §12.4).
4. `sc stop` on the supervisor while the server is running: server shuts down gracefully first.
5. Supervisor process killed: no surviving `python.exe` (Job Object), next `start` succeeds.
6. `start` with SDRConnect holding the RSP: `failed`, and the real error appears in the client UI.
7. `start` with out-of-range or unknown parameters: `bad_request`, nothing spawned.
8. Server unreachable from outside the home subnet/VPN subnet (firewall scope verified).
