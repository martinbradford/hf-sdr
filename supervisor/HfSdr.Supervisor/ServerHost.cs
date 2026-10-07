using System.Diagnostics;
using System.Text.Json.Nodes;
using Microsoft.Extensions.Options;
using NetMQ;
using NetMQ.Sockets;

namespace HfSdr.Supervisor;

public enum ServerState { Stopped, Starting, Running, Stopping, Failed }

/// <summary>Raised for a request the supervisor refuses; maps to a protocol error reply.</summary>
public sealed class BadRequestException(string message) : Exception(message);

/// <summary>
/// Owns the Python server child: spawn hidden in a Job Object, wait for the stdout banner, stop gracefully
/// via the server's own `shutdown` command (never a plain kill unless the graceful path times out).
/// See protocol/server_lifecycle.md §12.
/// </summary>
public sealed class ServerHost(IOptions<SupervisorOptions> options, ILogger<ServerHost> log) : IDisposable
{
    private const string Banner = "hf-sdr-server: control";
    private const int LogTailLines = 20;

    private readonly SupervisorOptions _opt = options.Value;
    private readonly object _gate = new();
    private readonly Queue<string> _tail = new();
    private readonly JobObject _job = new();

    private ServerState _state = ServerState.Stopped;
    private Process? _proc;
    private string? _token;
    private string? _lastError;
    private DateTime _startedUtc;
    private TaskCompletionSource<bool>? _bannerSeen;
    private Task? _worker;

    // ---- protocol-facing -------------------------------------------------------------------

    public JsonObject Status()
    {
        lock (_gate)
        {
            var o = new JsonObject { ["state"] = StateName() };
            if (_proc is { } p && _state is ServerState.Starting or ServerState.Running or ServerState.Stopping)
            {
                o["pid"] = p.Id;
                o["uptime_s"] = (int)(DateTime.UtcNow - _startedUtc).TotalSeconds;
                o["ports"] = new JsonObject
                {
                    ["control"] = _opt.ControlPort, ["stream"] = _opt.StreamPort, ["audio"] = _opt.AudioPort,
                };
            }
            if (_lastError is not null) o["last_error"] = _lastError;
            var tail = new JsonArray();
            foreach (var l in _tail) tail.Add(l);
            o["log_tail"] = tail;
            return o;
        }
    }

    /// <summary>Validate then begin starting. Returns immediately with the current state (idempotent).</summary>
    public JsonObject Start(JsonObject? p)
    {
        double center = _opt.DefaultCenterHz;
        string mode = "single";
        if (p is not null)
        {
            foreach (var kv in p)
            {
                switch (kv.Key)
                {
                    case "center_hz":
                        if (kv.Value is not JsonValue cv || !cv.TryGetValue(out center) || double.IsNaN(center) || center < 0 || center > 30e6)
                            throw new BadRequestException("center_hz must be a number within 0..30e6");
                        break;
                    case "tuner_mode":
                        if (kv.Value is not JsonValue mv || !mv.TryGetValue(out string? m) || (m != "single" && m != "diversity"))
                            throw new BadRequestException("tuner_mode must be 'single' or 'diversity'");
                        mode = m;
                        break;
                    default:
                        throw new BadRequestException($"unknown parameter: {kv.Key}");
                }
            }
        }

        lock (_gate)
        {
            // Accepted from stopped/failed only; otherwise a no-op that reports where we are.
            if (_state is ServerState.Stopped or ServerState.Failed)
            {
                _state = ServerState.Starting;
                _lastError = null;
                _tail.Clear();
                _worker = Task.Run(() => RunStartAsync(center, mode));
            }
            return new JsonObject { ["state"] = StateName() };
        }
    }

    /// <summary>Begin a graceful stop. Returns immediately (idempotent).</summary>
    public JsonObject Stop()
    {
        lock (_gate)
        {
            BeginStopLocked();
            return new JsonObject { ["state"] = StateName() };
        }
    }

    /// <summary>Service shutdown path: same graceful sequence, awaited.</summary>
    public async Task ShutdownAsync()
    {
        Task? t;
        lock (_gate)
        {
            BeginStopLocked();
            t = _worker;
        }
        if (t is not null) await t.ConfigureAwait(false);
    }

    private void BeginStopLocked()
    {
        if (_state is ServerState.Running or ServerState.Starting)
        {
            var starting = _worker;                       // a start still in flight must finish unwinding first
            _state = ServerState.Stopping;
            _worker = Task.Run(async () =>
            {
                if (starting is not null) { try { await starting.ConfigureAwait(false); } catch { } }
                await StopAsync().ConfigureAwait(false);
            });
        }
    }

    private string StateName() => _state.ToString().ToLowerInvariant();

    // ---- workers ---------------------------------------------------------------------------

    private async Task RunStartAsync(double centerHz, string mode)
    {
        try
        {
            if (!File.Exists(_opt.PythonExe)) throw new FileNotFoundException($"PythonExe not found: {_opt.PythonExe}");
            if (!File.Exists(_opt.ServerScript)) throw new FileNotFoundException($"ServerScript not found: {_opt.ServerScript}");

            // Token stays on loopback + in the child's env (not argv, so it is not visible in the process list).
            string token = Convert.ToHexString(System.Security.Cryptography.RandomNumberGenerator.GetBytes(16));

            var psi = new ProcessStartInfo(_opt.PythonExe)
            {
                UseShellExecute = false, CreateNoWindow = true,
                RedirectStandardOutput = true, RedirectStandardError = true,
                WorkingDirectory = Path.GetDirectoryName(_opt.ServerScript)!,
            };
            psi.ArgumentList.Add(_opt.ServerScript);
            psi.ArgumentList.Add("--center"); psi.ArgumentList.Add(centerHz.ToString("R", System.Globalization.CultureInfo.InvariantCulture));
            psi.ArgumentList.Add("--bind"); psi.ArgumentList.Add(_opt.ServerBind);
            psi.ArgumentList.Add("--control-port"); psi.ArgumentList.Add(_opt.ControlPort.ToString());
            psi.ArgumentList.Add("--stream-port"); psi.ArgumentList.Add(_opt.StreamPort.ToString());
            psi.ArgumentList.Add("--audio-port"); psi.ArgumentList.Add(_opt.AudioPort.ToString());
            psi.Environment["HF_SDR_SHUTDOWN_TOKEN"] = token;
            psi.Environment["PYTHONUNBUFFERED"] = "1";

            var banner = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
            var proc = new Process { StartInfo = psi, EnableRaisingEvents = true };
            proc.OutputDataReceived += (_, e) => OnLine(e.Data, banner);
            proc.ErrorDataReceived += (_, e) => OnLine(e.Data, null);
            proc.Exited += (_, _) => OnExited(proc, banner);

            lock (_gate) { _bannerSeen = banner; _proc = proc; _token = token; _startedUtc = DateTime.UtcNow; }
            proc.Start();
            try { _job.Assign(proc); }
            catch (Exception ex) { log.LogWarning(ex, "could not assign server to job object"); }
            proc.BeginOutputReadLine();
            proc.BeginErrorReadLine();
            log.LogInformation("server spawned, pid {Pid}", proc.Id);

            var done = await Task.WhenAny(banner.Task, Task.Delay(TimeSpan.FromSeconds(_opt.StartTimeoutSec))).ConfigureAwait(false);
            if (done != banner.Task)
            {
                // Timed out with the process still alive. A kill here may skip device deinit; there is no
                // control socket to ask nicely (or the server is wedged), so it is the only option.
                lock (_gate) { if (_state != ServerState.Starting) return; }
                Fail($"server did not become ready within {_opt.StartTimeoutSec} s");
                KillTree(proc);
                return;
            }
            if (!banner.Task.Result) return;              // process exited first; OnExited already set Failed/Stopped
        }
        catch (Exception ex)
        {
            Fail(ex.Message);
            return;
        }

        lock (_gate) { if (_state != ServerState.Starting) return; }

        if (mode == "diversity")
        {
            // The server has no --tuner-mode flag; apply it over loopback once it is up. The server itself
            // falls back to single on dual-tuner init failure, so a failed switch is a warning, not a failure.
            try
            {
                var reply = await Task.Run(() => Request("set_tuner_mode", new JsonObject { ["mode"] = "diversity" },
                    TimeSpan.FromSeconds(_opt.ModeSwitchTimeoutSec))).ConfigureAwait(false);
                log.LogInformation("set_tuner_mode diversity -> {Reply}", reply?.ToJsonString());
                if (reply?["ok"]?.GetValue<bool>() != true)
                    lock (_gate) { _lastError = "diversity requested but not applied: " + reply?["error"]?["message"]; }
            }
            catch (Exception ex)
            {
                lock (_gate) { _lastError = "diversity requested but not applied: " + ex.Message; }
            }
        }

        lock (_gate) { if (_state == ServerState.Starting) _state = ServerState.Running; }
    }

    private async Task StopAsync()
    {
        Process? proc; string? token;
        lock (_gate) { proc = _proc; token = _token; }
        if (proc is null || proc.HasExited) { MarkStopped(); return; }

        // 1. Graceful: the server's own `shutdown` (srv.stop(); srv.wait() => SDRPlay device deinit).
        //    Killing instead is what wedges the RSP (design §4.1/§12.4).
        string? note = null;
        try
        {
            var reply = await Task.Run(() => Request("shutdown", new JsonObject { ["token"] = token }, TimeSpan.FromSeconds(2))).ConfigureAwait(false);
            if (reply?["ok"]?.GetValue<bool>() != true) note = "shutdown refused: " + reply?["error"]?["message"];
        }
        catch (Exception ex) { note = "shutdown request failed: " + ex.Message; }

        // 2. Wait for exit.
        try
        {
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(_opt.GracefulStopSec));
            await proc.WaitForExitAsync(cts.Token).ConfigureAwait(false);
        }
        catch (OperationCanceledException) { }

        // 3. Last resort.
        if (!proc.HasExited)
        {
            note ??= $"server did not exit within {_opt.GracefulStopSec} s";
            log.LogWarning("{Note}; terminating — the RSP may need a power-cycle", note);
            KillTree(proc);
            note += " (terminated; RSP may need a power-cycle)";
        }
        MarkStopped(note);
    }

    // ---- helpers ---------------------------------------------------------------------------

    private void OnLine(string? line, TaskCompletionSource<bool>? banner)
    {
        if (line is null) return;
        lock (_gate)
        {
            _tail.Enqueue(line);
            while (_tail.Count > LogTailLines) _tail.Dequeue();
        }
        if (banner is not null && line.StartsWith(Banner, StringComparison.Ordinal)) banner.TrySetResult(true);
    }

    private void OnExited(Process proc, TaskCompletionSource<bool> banner)
    {
        int code = -1;
        try { code = proc.ExitCode; } catch { }
        lock (_gate)
        {
            banner.TrySetResult(false);
            if (!ReferenceEquals(proc, _proc)) return;
            if (_state is ServerState.Starting or ServerState.Running)
            {
                _lastError = code == 2 ? "server exited (code 2): port clash — is another server already running?"
                                       : $"server exited unexpectedly (code {code})";
                _state = ServerState.Failed;              // never auto-restart (design §12.3)
                log.LogError("{Err}", _lastError);
            }
            // Stopping: StopAsync finishes the transition.
        }
    }

    private void Fail(string error)
    {
        lock (_gate) { _lastError = error; _state = ServerState.Failed; }
        log.LogError("start failed: {Err}", error);
    }

    private void MarkStopped(string? note = null)
    {
        lock (_gate) { if (note is not null) _lastError = note; _state = ServerState.Stopped; _proc = null; _token = null; }
    }

    private static void KillTree(Process p)
    {
        try { p.Kill(entireProcessTree: true); } catch { }
    }

    /// <summary>One-shot REQ on loopback to the server's control port.</summary>
    private JsonNode? Request(string cmd, JsonObject args, TimeSpan timeout)
    {
        using var req = new RequestSocket();
        req.Options.Linger = TimeSpan.Zero;
        req.Connect($"tcp://127.0.0.1:{_opt.ControlPort}");
        var msg = new JsonObject { ["id"] = 1, ["cmd"] = cmd, ["params"] = args };
        req.SendFrame(msg.ToJsonString());
        if (!req.TryReceiveFrameString(timeout, out var reply)) throw new TimeoutException($"{cmd}: no reply within {timeout.TotalSeconds:0} s");
        return JsonNode.Parse(reply!);
    }

    public void Dispose() => _job.Dispose();
}
