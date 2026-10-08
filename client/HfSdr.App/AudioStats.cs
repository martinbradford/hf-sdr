using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using NAudio.Wave;

namespace HfSdr.App;

/// <summary>Point-in-time copy of the audio-path counters.</summary>
public readonly record struct AudioStatsSnapshot(
    long Frames, long LostFrames, long GapEvents, long SeqResets,
    long Stalls, double MaxGapMs,
    long Underruns, double UnderrunMs,
    long Resyncs, double DiscardedMs);

/// <summary>
/// Tells "lost" audio from "late" audio (design: protocol/bandwidth_design.md §6).
///   Lost   : a hole in the per-VRX <c>seq</c> — the frame never reached us (dropped by the
///            server's send queue, or by ZeroMQ when the link couldn't keep up).
///   Late   : the frame arrived, but the gap since the previous one exceeded <see cref="StallMs"/>
///            (Wi-Fi/TCP stall followed by a burst). Not lost, but it can still starve playback.
///   Dry    : playback asked for more audio than was buffered, so silence was inserted.
///   Resync : the backlog grew past the cap and was discarded to get back to real time.
/// Thread-safe: frames arrive on the network thread, playback reads on the audio thread.
/// </summary>
public sealed class AudioStats
{
    /// <summary>Inter-arrival gap treated as a stall. Normal frames are ~20 ms apart.</summary>
    public const double StallMs = 100;
    /// <summary>Audio "is flowing" if a frame arrived within this long; gates dry-read counting so an
    /// idle receiver (no VRX, nothing to play) is not reported as a stream of underruns.</summary>
    private const double ActiveWindowMs = 1000;

    private readonly Func<double> _nowMs;
    private readonly object _lock = new();
    private readonly Dictionary<int, (long Seq, double AtMs)> _last = new();
    private double _lastAnyMs = double.NegativeInfinity;

    private long _frames, _lostFrames, _gapEvents, _seqResets, _stalls, _underruns, _resyncs;
    private double _maxGapMs, _underrunMs, _discardedMs;

    public AudioStats() : this(null) { }

    /// <param name="nowMs">Monotonic clock in ms (injectable for tests).</param>
    public AudioStats(Func<double>? nowMs)
    {
        var sw = Stopwatch.StartNew();
        _nowMs = nowMs ?? (() => sw.Elapsed.TotalMilliseconds);
    }

    /// <summary>Call for every audio frame received (network thread).</summary>
    public void OnFrame(int vrxId, long seq)
    {
        double now = _nowMs();
        lock (_lock)
        {
            _frames++;
            _lastAnyMs = now;
            if (_last.TryGetValue(vrxId, out var prev))
            {
                // Only compare against the same VRX: a freshly added VRX has no baseline, so the
                // silence before it existed is never mistaken for a stall.
                double gap = now - prev.AtMs;
                if (gap > _maxGapMs) _maxGapMs = gap;
                if (gap > StallMs) _stalls++;

                if (seq > prev.Seq + 1) { _gapEvents++; _lostFrames += seq - prev.Seq - 1; }
                else if (seq <= prev.Seq) _seqResets++;      // restart or reorder; not counted as loss
            }
            _last[vrxId] = (seq, now);
        }
    }

    /// <summary>True while audio frames are arriving (used to ignore idle underruns).</summary>
    public bool StreamActive
    {
        get { lock (_lock) return _nowMs() - _lastAnyMs < ActiveWindowMs; }
    }

    /// <summary>Playback was handed <paramref name="silenceMs"/> of inserted silence (audio thread).</summary>
    public void OnDryRead(double silenceMs)
    {
        lock (_lock) { _underruns++; _underrunMs += silenceMs; }
    }

    /// <summary>The backlog was cleared; <paramref name="discardedMs"/> of audio was thrown away.</summary>
    public void OnResync(double discardedMs)
    {
        lock (_lock) { _resyncs++; _discardedMs += discardedMs; }
    }

    public AudioStatsSnapshot Snapshot()
    {
        lock (_lock)
            return new(_frames, _lostFrames, _gapEvents, _seqResets, _stalls, _maxGapMs,
                       _underruns, _underrunMs, _resyncs, _discardedMs);
    }
}

/// <summary>
/// Wraps the playback buffer and records every read that had to be padded with silence.
/// Behaviour is unchanged: the read is delegated untouched (BufferedWaveProvider pads with zeros
/// itself); we only look at how much was buffered first. Any padding ends up in the audible stream,
/// so this counts real glitches regardless of how the output API chunks its reads.
/// </summary>
public sealed class MeteredWaveProvider : IWaveProvider
{
    private readonly BufferedWaveProvider _inner;
    private readonly Func<AudioStats> _stats;

    /// <param name="stats">Resolved on every read, so a reconnect that swaps the stats object is followed.</param>
    public MeteredWaveProvider(BufferedWaveProvider inner, Func<AudioStats> stats)
    {
        _inner = inner;
        _stats = stats;
    }

    public WaveFormat WaveFormat => _inner.WaveFormat;

    public int Read(byte[] buffer, int offset, int count)
    {
        int have = _inner.BufferedBytes;
        if (have < count)
        {
            var s = _stats();
            if (s.StreamActive)
                s.OnDryRead((count - have) * 1000.0 / _inner.WaveFormat.AverageBytesPerSecond);
        }
        return _inner.Read(buffer, offset, count);
    }
}

/// <summary>Appends audio-glitch events to %APPDATA%\HfSdr\audio-events.log, so rare dropouts can be
/// reviewed after the fact. Best effort; never throws; stops growing at 2 MB.</summary>
public static class AudioEventLog
{
    private const long MaxBytes = 2_000_000;

    public static string PathFile => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "HfSdr", "audio-events.log");

    public static void Append(string line)
    {
        try
        {
            var p = PathFile;
            Directory.CreateDirectory(Path.GetDirectoryName(p)!);
            if (File.Exists(p) && new FileInfo(p).Length > MaxBytes) return;
            File.AppendAllText(p, $"{DateTime.Now:yyyy-MM-dd HH:mm:ss}  {line}{Environment.NewLine}");
        }
        catch { /* diagnostics must never affect the app */ }
    }
}
