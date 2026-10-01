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

## 2. Why not genuinely embed the server

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
- **Run the server as a Windows service** for a headless shack box — different owner model;
  revisit only if wanted.
- Should the client **restart** a server that died unexpectedly, or just report it? Leaning
  report-and-offer-Retry: silent restarts hide device problems, and an auto-restart loop against
  a wedged RSP is worse than a stopped app.
- Does the spawned server want a **log file** (alongside piped stdout) so a user can send a
  diagnostic after the GUI has gone? Probably yes, once there are users.
- Should `--center` and tuner mode at spawn come from **persisted client settings** rather than
  the current hardcoded default? Ties into the persistence work already on the roadmap.
