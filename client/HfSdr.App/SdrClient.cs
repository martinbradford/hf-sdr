using System;
using System.Text.Json;
using System.Threading;
using NetMQ;
using NetMQ.Sockets;

namespace HfSdr.App;

/// <summary>One spectrum frame: magnitudes (dBFS) low→high across the span.</summary>
public record SpectrumFrame(int FftSize, long CenterHz, long SpanHz, float[] Mags);

/// <summary>
/// Client for the HF SDR headless server (protocol/messages.md v0.1).
///   control : REQ/REP  :5555  JSON {id,cmd,params} -> {id,ok,result|error}
///   stream  : SUB       :5556  spectrum/<src>  header + float32[fft_size] dBFS
///   audio   : SUB       :5557  audio/<vrx_id>  header + int16 LE mono
/// </summary>
public sealed class SdrClient : IDisposable
{
    private RequestSocket? _control;
    private SubscriberSocket? _spectrum;
    private SubscriberSocket? _audio;
    private NetMQPoller? _poller;
    private readonly object _ctrlLock = new();
    private int _reqId;

    public event Action<SpectrumFrame>? SpectrumReceived;
    public event Action<short[]>? AudioReceived;

    public void Connect(string host = "localhost")
    {
        _control = new RequestSocket();
        _control.Connect($"tcp://{host}:5555");

        _spectrum = new SubscriberSocket();
        _spectrum.Connect($"tcp://{host}:5556");
        _spectrum.Subscribe("spectrum/");
        _spectrum.ReceiveReady += OnSpectrum;

        _audio = new SubscriberSocket();
        _audio.Connect($"tcp://{host}:5557");
        _audio.Subscribe("audio/");
        _audio.ReceiveReady += OnAudio;

        _poller = new NetMQPoller { _spectrum, _audio };
        _poller.RunAsync();
    }

    /// <summary>Synchronous control request. Returns the "result" element (cloned).</summary>
    public JsonElement Send(string cmd, object? parameters = null)
    {
        lock (_ctrlLock)
        {
            if (_control is null) throw new InvalidOperationException("not connected");
            var req = new { id = Interlocked.Increment(ref _reqId), cmd, @params = parameters ?? new { } };
            _control.SendFrame(JsonSerializer.Serialize(req));

            if (!_control.TryReceiveFrameString(TimeSpan.FromSeconds(3), out var reply) || reply is null)
                throw new TimeoutException($"no reply to '{cmd}'");

            using var doc = JsonDocument.Parse(reply);
            var root = doc.RootElement;
            if (!root.GetProperty("ok").GetBoolean())
            {
                var err = root.GetProperty("error");
                throw new InvalidOperationException(
                    $"{err.GetProperty("code").GetString()}: {err.GetProperty("message").GetString()}");
            }
            return root.TryGetProperty("result", out var res) ? res.Clone() : default;
        }
    }

    private void OnSpectrum(object? sender, NetMQSocketEventArgs e)
    {
        var msg = e.Socket.ReceiveMultipartMessage();
        if (msg.FrameCount < 3) return;
        using var header = JsonDocument.Parse(msg[1].ConvertToString());
        var h = header.RootElement;
        int fftSize = h.GetProperty("fft_size").GetInt32();
        long center = h.GetProperty("center_hz").GetInt64();
        long span = h.GetProperty("span_hz").GetInt64();

        var bytes = msg[2].ToByteArray();
        var mags = new float[bytes.Length / sizeof(float)];
        Buffer.BlockCopy(bytes, 0, mags, 0, mags.Length * sizeof(float));  // little-endian both ends
        SpectrumReceived?.Invoke(new SpectrumFrame(fftSize, center, span, mags));
    }

    private void OnAudio(object? sender, NetMQSocketEventArgs e)
    {
        var msg = e.Socket.ReceiveMultipartMessage();
        if (msg.FrameCount < 3) return;
        var bytes = msg[2].ToByteArray();
        var samples = new short[bytes.Length / sizeof(short)];
        Buffer.BlockCopy(bytes, 0, samples, 0, samples.Length * sizeof(short));
        AudioReceived?.Invoke(samples);
    }

    public void Dispose()
    {
        try { _poller?.Stop(); } catch { /* ignore */ }
        _poller?.Dispose();
        _control?.Dispose();
        _spectrum?.Dispose();
        _audio?.Dispose();
    }
}
