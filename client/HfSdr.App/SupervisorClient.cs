using System;
using System.Collections.Generic;
using System.Text.Json;
using NetMQ;
using NetMQ.Sockets;

namespace HfSdr.App;

/// <summary>Snapshot of the supervisor's view of the server (protocol/server_lifecycle.md §12.3).</summary>
public record SupervisorStatus(string State, int ControlPort, int StreamPort, int AudioPort,
                               string? LastError, IReadOnlyList<string> LogTail)
{
    public bool IsRunning => State == "running";
    public bool IsFailed => State == "failed";
}

/// <summary>
/// Talks to the supervisor service on the PC that has the RSP (default :5554). Every call uses a throwaway
/// REQ socket: a timed-out REQ socket is wedged by ZeroMQ's send/receive alternation, so a probe must
/// never reuse one (design §4.3).
/// </summary>
public sealed class SupervisorClient(string host, int port = 5554)
{
    private int _reqId;

    /// <summary>Throws <see cref="TimeoutException"/> if the supervisor is unreachable.</summary>
    public SupervisorStatus Status(int timeoutMs = 2000) => Parse(Call("status", null, timeoutMs));

    /// <summary>Asks the supervisor to launch the server; returns at once (poll <see cref="Status"/>).</summary>
    public void Start(long centerHz, string tunerMode, int timeoutMs = 3000) =>
        Call("start", new { center_hz = centerHz, tuner_mode = tunerMode }, timeoutMs);

    public void Stop(int timeoutMs = 3000) => Call("stop", null, timeoutMs);

    private JsonElement Call(string cmd, object? parameters, int timeoutMs)
    {
        using var req = new RequestSocket();
        req.Options.Linger = TimeSpan.Zero;
        req.Connect($"tcp://{host}:{port}");
        req.SendFrame(JsonSerializer.Serialize(new { id = ++_reqId, cmd, @params = parameters ?? new { } }));
        if (!req.TryReceiveFrameString(TimeSpan.FromMilliseconds(timeoutMs), out var reply) || reply is null)
            throw new TimeoutException($"no reply from supervisor at {host}:{port}");

        using var doc = JsonDocument.Parse(reply);
        var root = doc.RootElement;
        if (!root.GetProperty("ok").GetBoolean())
        {
            var err = root.GetProperty("error");
            throw new InvalidOperationException($"supervisor: {err.GetProperty("message").GetString()}");
        }
        return root.GetProperty("result").Clone();
    }

    private static SupervisorStatus Parse(JsonElement r)
    {
        int Port(string name, int dflt) =>
            r.TryGetProperty("ports", out var p) && p.TryGetProperty(name, out var v) ? v.GetInt32() : dflt;

        var tail = new List<string>();
        if (r.TryGetProperty("log_tail", out var lt))
            foreach (var l in lt.EnumerateArray()) tail.Add(l.GetString() ?? "");

        return new SupervisorStatus(
            r.GetProperty("state").GetString() ?? "?",
            Port("control", 5555), Port("stream", 5556), Port("audio", 5557),
            r.TryGetProperty("last_error", out var le) ? le.GetString() : null,
            tail);
    }
}
