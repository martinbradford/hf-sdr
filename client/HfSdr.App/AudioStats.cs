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
    long Underruns, double UnderrunMs, long MicroShortfalls,
    long Resyncs, double DiscardedMs);

/// <summary>
/// Tells "lost" audio from "late" audio (design: protocol/bandwidth_design.md §6).
///   Lost   : a hole in the per-VRX <c>seq</c> — the frame never reached us (dropped by the
///            server's send queue, or by ZeroMQ when the link couldn't keep up).
///   Late   : the frame arrived, but the gap since the previous one exceeded <see cref="StallMs"/>
///            (Wi-Fi/TCP stall followed by a burst). Not lost, but it can still starve playback.
///   Dry    : playback ran out of audio for at least <see cref="DryThresholdMs"/> and silence was
///            inserted. Only counted if audio then resumes (see <see cref="OnDryRead"/>), so the
///            end of a stream is not reported as a glitch.
///   Micro  : a shortfall under <see cref="DryThresholdMs"/> (a few samples). Tracked, not alarming.
///   Resync : the backlog grew past the cap and was discarded to get back to real time.
/// Thread-safe: frames arrive on the network thread, playback reads on the audio thread.
/// </summary>
public sealed class AudioStats
{
    /// <summary>Inter-arrival gap treated as a stall. Normal frames are ~20 ms apart.</summary>
    public const double StallMs = 100;
    /// <summary>Smallest inserted silence counted as a dry read; shorter shortfalls are "micro".</summary>
    public const double DryThresholdMs = 2;
    /// <summary>A dry read is confirmed if a frame arrives within this long afterwards.</summary>
    private const double CommitWindowMs = 1000;

    private readonly Func<double> _nowMs;
    private readonly object _lock = new();
    private readonly Dictionary<int, (long Seq, double AtMs)> _last = new();

    private long _frames, _lostFrames, _gapEvents, _seqResets, _stalls, _underruns, _micro, _resyncs;
    private double _maxGapMs, _underrunMs, _discardedMs;

    // Dry reads wait here until audio resumes; if it never does the stream simply ended.
    private long _pendDry;
    private double _pendDryMs, _pendAtMs = double.NegativeInfinity;

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

            // Audio resumed shortly after playback ran dry: that was a real glitch, not a stream end.
            if (_pendDry > 0)
            {
                if (now - _pendAtMs <= CommitWindowMs) { _underruns += _pendDry; _underrunMs += _pendDryMs; }
                _pendDry = 0; _pendDryMs = 0;
            }

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

    /// <summary>Playback was handed <paramref name="silenceMs"/> (>= <see cref="DryThresholdMs"/>) of
    /// inserted silence (audio thread). Held as pending until the next frame confirms audio resumed.</summary>
    public void OnDryRead(double silenceMs)
    {
        double now = _nowMs();
        lock (_lock)
        {
            if (_pendDry > 0 && now - _pendAtMs > CommitWindowMs) { _pendDry = 0; _pendDryMs = 0; }
            _pendDry++; _pendDryMs += silenceMs; _pendAtMs = now;
        }
    }

    /// <summary>A shortfall below <see cref="DryThresholdMs"/>: a few padded samples.</summary>
    public void OnMicroShortfall()
    {
        lock (_lock) _micro++;
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
                       _underruns, _underrunMs, _micro, _resyncs, _discardedMs);
    }
}

/// <summary>
/// The playback side of the audio path: a small jitter cushion plus glitch metering, wrapped around
/// the <see cref="BufferedWaveProvider"/> the output reads from.
///
/// Priming: until at least <c>primeMs</c> of audio is buffered, reads return silence WITHOUT
/// consuming anything, so the buffer fills to a cushion first. Steady state then holds roughly that
/// much audio, which absorbs network jitter up to the cushion size. After a real dry spell it
/// re-primes the same way. The price is <c>primeMs</c> of extra latency; 0 disables the cushion.
///
/// Metering: a shortfall (playback wanted more than was buffered) is padded with silence exactly as
/// BufferedWaveProvider's ReadFully would. Padding of at least <see cref="AudioStats.DryThresholdMs"/>
/// is reported as a dry read; smaller ones as micro shortfalls. Reads while priming are not glitches
/// and are not counted.
/// </summary>
public sealed class PrimedWaveProvider : IWaveProvider
{
    private readonly BufferedWaveProvider _inner;
    private readonly Func<AudioStats> _stats;
    private readonly Func<int> _primeMs;
    private bool _primed;

    /// <param name="stats">Resolved on every read, so a reconnect that swaps the stats object is followed.</param>
    /// <param name="primeMs">Cushion in ms, read on every call so a settings change applies live.</param>
    public PrimedWaveProvider(BufferedWaveProvider inner, Func<AudioStats> stats, Func<int> primeMs)
    {
        _inner = inner;
        _stats = stats;
        _primeMs = primeMs;
    }

    public WaveFormat WaveFormat => _inner.WaveFormat;

    public int Read(byte[] buffer, int offset, int count)
    {
        double bytesPerMs = _inner.WaveFormat.AverageBytesPerSecond / 1000.0;
        int have = _inner.BufferedBytes;

        if (!_primed)
        {
            int block = _inner.WaveFormat.BlockAlign;
            int need = (int)(Math.Max(0, _primeMs()) * bytesPerMs);
            need = Math.Max(need, count) / block * block;            // at least this read, block-aligned
            if (have < need)
            {
                Array.Clear(buffer, offset, count);                  // cushion still filling: play silence
                return count;
            }
            _primed = true;
        }

        if (have < count)
        {
            double silenceMs = (count - have) / bytesPerMs;
            var s = _stats();
            if (silenceMs >= AudioStats.DryThresholdMs)
            {
                s.OnDryRead(silenceMs);
                _primed = false;                                      // real dry spell: rebuild the cushion
            }
            else s.OnMicroShortfall();
        }
        return _inner.Read(buffer, offset, count);                    // pads any shortfall with silence
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
