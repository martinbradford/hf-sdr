using System;
using System.Collections.Generic;
using System.Text.Json;
using System.Threading.Tasks;
using Avalonia.Controls;
using Avalonia.Input;
using Avalonia.Interactivity;
using Avalonia.Media;
using Avalonia.Threading;
using NAudio.CoreAudioApi;
using NAudio.Wave;

namespace HfSdr.App;

public partial class MainWindow : Window
{
    private SdrClient _client = new();
    private readonly ClientSettings _settings = ClientSettings.Load();
    private SupervisorClient? _supervisor;      // set while connected to a supervisor-launched receiver
    private bool _connecting;
    private WaterfallRenderer? _wf;
    private readonly MMDeviceEnumerator _mmEnum = new();
    private readonly List<MMDevice?> _renderDevices = new();   // parallel to AudioDeviceBox; null = Windows default
    private IWavePlayer? _waveOut;
    private BufferedWaveProvider? _audioBuf;
    private bool _connected;

    private long _centerHz = 7_150_000;          // from the Freq box / Tune
    private long _dispCenter = 7_150_000;         // current display window (from frames)
    private long _dispSpan = 250_000;

    private int? _vrxId;                          // the single click-tuned VRX
    private string _vrxMode = "lsb";
    private long _vrxFreq;
    private bool _busy;

    private float[]? _lastMags;                   // most recent spectrum, for null-width estimation
    private long _nullTargetFreq;                 // frequency under the last right-click
    private bool _nullActive;
    private long _nullCenterHz, _nullWidthHz;     // engaged null's target band (for the marker)
    private DispatcherTimer? _nullTimer;          // polls get_status to animate null_depth_db

    private const int WaterfallWidth = 1024;
    private const int WaterfallHeight = 320;

    public MainWindow()
    {
        InitializeComponent();
        PopulateAudioDevices();
        HostBox.Text = _settings.Host;
        SupervisorBox.IsChecked = _settings.UseSupervisor;
        ReleaseBox.IsChecked = _settings.ReleaseOnClose;
    }

    /// <summary>List the active WASAPI render endpoints; index 0 is the Windows default.</summary>
    private void PopulateAudioDevices()
    {
        AudioDeviceBox.Items.Add("Default (Windows)");
        _renderDevices.Add(null);
        foreach (var d in _mmEnum.EnumerateAudioEndPoints(DataFlow.Render, DeviceState.Active))
        {
            AudioDeviceBox.Items.Add(d.FriendlyName);
            _renderDevices.Add(d);
        }
        AudioDeviceBox.SelectedIndex = 0;
    }

    /// <summary>Resolve the selected endpoint; "Default" tracks the current system default.</summary>
    private MMDevice SelectedRenderDevice()
    {
        int i = AudioDeviceBox.SelectedIndex;
        var dev = (i >= 0 && i < _renderDevices.Count) ? _renderDevices[i] : null;
        return dev ?? _mmEnum.GetDefaultAudioEndpoint(DataFlow.Render, Role.Multimedia);
    }

    /// <summary>(Re)create the output player on the currently selected device.</summary>
    private void StartAudioOut()
    {
        _waveOut?.Dispose();
        _audioBuf = new BufferedWaveProvider(new WaveFormat(48_000, 16, 1))
        {
            BufferDuration = TimeSpan.FromSeconds(2),   // ceiling; catch-up keeps latency low
            DiscardOnBufferOverflow = true
        };
        // Shared mode; NAudio resamples our 48k/16/mono buffer to the endpoint's mix format.
        _waveOut = new WasapiOut(SelectedRenderDevice(), AudioClientShareMode.Shared, true, 150);
        _waveOut.Init(new PrimedWaveProvider(_audioBuf, () => _client.Audio,
                                             () => Math.Clamp(_settings.AudioPrimeMs, 0, 300)));
        _waveOut.Play();
    }

    // ---- audio loss/lateness metrics (protocol/bandwidth_design.md §6) ----
    private DispatcherTimer? _statsTimer;
    private AudioStatsSnapshot _lastStats;

    private void StartAudioStatsTimer()
    {
        _lastStats = default;
        AudioStatsLabel.Text = "audio —";
        AudioStatsLabel.Foreground = Brushes.Gray;
        _statsTimer ??= new DispatcherTimer { Interval = TimeSpan.FromSeconds(1) };
        _statsTimer.Tick -= AudioStatsTick;
        _statsTimer.Tick += AudioStatsTick;
        _statsTimer.Start();
    }

    private void AudioStatsTick(object? sender, EventArgs e)
    {
        var s = _client.Audio.Snapshot();
        double bufMs = _audioBuf?.BufferedDuration.TotalMilliseconds ?? 0;
        bool bad = s.LostFrames + s.Stalls + s.Underruns + s.Resyncs > 0;
        AudioStatsLabel.Text = $"audio lost {s.LostFrames}  late {s.Stalls}  dry {s.Underruns}  resync {s.Resyncs}";
        AudioStatsLabel.Foreground = bad ? Brushes.Orange : Brushes.MediumSeaGreen;
        ToolTip.SetTip(AudioStatsLabel,
            $"lost  = frames missing from the sequence ({s.GapEvents} gap(s)); never arrived\n" +
            $"late  = {s.Stalls} stall(s) of >{AudioStats.StallMs:0} ms between frames (max gap {s.MaxGapMs:0} ms)\n" +
            $"dry   = {s.Underruns} playback read(s) padded with >= {AudioStats.DryThresholdMs:0} ms of silence ({s.UnderrunMs:0} ms total)\n" +
            $"micro = {s.MicroShortfalls} shortfall(s) under {AudioStats.DryThresholdMs:0} ms (a few samples; ignored)\n" +
            $"cushion = {Math.Clamp(_settings.AudioPrimeMs, 0, 300)} ms (AudioPrimeMs in settings.json)\n" +
            $"resync= {s.Resyncs} backlog discard(s) ({s.DiscardedMs:0} ms of audio dropped)\n" +
            $"frames={s.Frames}  seq resets={s.SeqResets}  buffered now={bufMs:0} ms\n" +
            $"Events are logged to {AudioEventLog.PathFile}");

        // Log only when something new happened since the last tick, so rare dropouts leave a trail.
        var p = _lastStats;
        if (s.LostFrames != p.LostFrames || s.Stalls != p.Stalls || s.Underruns != p.Underruns || s.Resyncs != p.Resyncs)
        {
            AudioEventLog.Append(
                $"+lost {s.LostFrames - p.LostFrames}  +late {s.Stalls - p.Stalls}  " +
                $"+dry {s.Underruns - p.Underruns} ({s.UnderrunMs - p.UnderrunMs:0} ms)  " +
                $"+resync {s.Resyncs - p.Resyncs} ({s.DiscardedMs - p.DiscardedMs:0} ms)  " +
                $"maxgap {s.MaxGapMs:0} ms  buffered {bufMs:0} ms");
        }
        _lastStats = s;
    }

    private void OnAudioDeviceChanged(object? sender, SelectionChangedEventArgs e)
    {
        if (!_connected) return;   // just remember the choice until we connect
        try
        {
            StartAudioOut();
            StatusText.Text = $"Audio output → {AudioDeviceBox.SelectedItem}";
        }
        catch (Exception ex) { StatusText.Text = "Audio device switch failed: " + ex.Message; }
    }

    // ---- gain controls ------------------------------------------------
    private bool _suppressGain;   // guard: programmatic slider/checkbox updates must not send
    private bool _gainDirty;
    private bool _gainPushing;
    private int[]? _rfSteps;      // discrete RF reductions (dB); when set, the slider is index-based

    private static int GainInt(JsonElement g, string name, int fallback) =>
        g.TryGetProperty(name, out var v) ? v.GetInt32() : fallback;

    /// <summary>Index of the RF step closest to a given reduction (dB).</summary>
    private int NearestRfIndex(int db)
    {
        if (_rfSteps is null || _rfSteps.Length == 0) return 0;
        int best = 0;
        for (int i = 1; i < _rfSteps.Length; i++)
            if (Math.Abs(_rfSteps[i] - db) < Math.Abs(_rfSteps[best] - db)) best = i;
        return best;
    }

    /// <summary>Seed the gain UI from a server status "gain" block (ranges + values).</summary>
    private void InitGainControls(JsonElement g)
    {
        _suppressGain = true;
        // RF gain is discrete LNA steps. If the server lists them, drive the slider
        // by step index (one even detent per step) rather than a coarse dB range.
        _rfSteps = null;
        if (g.TryGetProperty("rf_gr_db_steps", out var rs) && rs.ValueKind == JsonValueKind.Array
            && rs.GetArrayLength() > 1)
        {
            var steps = new int[rs.GetArrayLength()];
            for (int i = 0; i < steps.Length; i++) steps[i] = rs[i].GetInt32();
            _rfSteps = steps;
            RfSlider.Minimum = 0;
            RfSlider.Maximum = steps.Length - 1;
            RfSlider.TickFrequency = 1;
            RfSlider.IsSnapToTickEnabled = true;
        }
        else if (g.TryGetProperty("rf_gr_db_range", out var rr) && rr.GetArrayLength() == 2)
        {
            RfSlider.IsSnapToTickEnabled = false;
            RfSlider.Minimum = rr[0].GetDouble();
            RfSlider.Maximum = rr[1].GetDouble();
        }
        if (g.TryGetProperty("if_gr_db_range", out var ir) && ir.GetArrayLength() == 2)
        {
            IfSlider.Minimum = ir[0].GetDouble();
            IfSlider.Maximum = ir[1].GetDouble();
        }
        RfSlider.Value = _rfSteps is null ? GainInt(g, "rf_gr_db", 0)
                                          : NearestRfIndex(GainInt(g, "rf_gr_db", 0));
        IfSlider.Value = GainInt(g, "if_gr_db", 40);
        AgcBox.IsChecked = !g.TryGetProperty("agc", out var agc) || agc.GetBoolean();
        _suppressGain = false;
        ShowGain(g);
    }

    /// <summary>Reflect an applied gain block in the labels and (snapped) RF slider.</summary>
    private void ShowGain(JsonElement g)
    {
        bool agc = !g.TryGetProperty("agc", out var a) || a.GetBoolean();
        int rf = GainInt(g, "rf_gr_db", (int)RfSlider.Value);
        int ifg = GainInt(g, "if_gr_db", (int)IfSlider.Value);
        int lna = GainInt(g, "lna_state", -1);
        _suppressGain = true;
        RfSlider.Value = _rfSteps is null ? rf : NearestRfIndex(rf);   // show the driver-snapped step
        _suppressGain = false;
        RfLabel.Text = lna >= 0 ? $"{rf} dB (LNA {lna})" : $"{rf} dB";
        IfLabel.Text = $"{ifg} dB";
        IfSlider.IsEnabled = !agc;                 // IF is manual only when AGC is off
    }

    private void OnAgcChanged(object? sender, RoutedEventArgs e) => QueueGain();
    private void OnRfGainChanged(object? sender, Avalonia.Controls.Primitives.RangeBaseValueChangedEventArgs e) => QueueGain();
    private void OnIfGainChanged(object? sender, Avalonia.Controls.Primitives.RangeBaseValueChangedEventArgs e) => QueueGain();

    private void QueueGain()
    {
        if (!_connected || _suppressGain) return;
        _gainDirty = true;
        PushGain();
    }

    /// <summary>Coalesced sender: pushes the latest gain state, never overlapping requests.</summary>
    private async void PushGain()
    {
        if (_gainPushing) return;
        _gainPushing = true;
        try
        {
            while (_connected && _gainDirty)
            {
                _gainDirty = false;
                bool agc = AgcBox.IsChecked == true;
                int rfIdx = (int)Math.Round(RfSlider.Value);
                int rf = _rfSteps is null ? rfIdx
                       : _rfSteps[Math.Clamp(rfIdx, 0, _rfSteps.Length - 1)];
                int ifg = (int)Math.Round(IfSlider.Value);
                var res = await Task.Run(() =>
                    _client.Send("set_gain", new { agc, rf_gr_db = rf, if_gr_db = ifg }));
                ShowGain(res);
            }
        }
        catch (Exception ex) { StatusText.Text = "gain failed: " + ex.Message; }
        finally { _gainPushing = false; }
    }

    // ---- tuner mode (single / diversity) ------------------------------
    private bool _suppressMode;    // guard: programmatic radio updates must not trigger a switch
    private bool _modeSwitching;

    /// <summary>Seed all mode-dependent UI from a status snapshot (mode radios,
    /// gain, and the restored VRX). Used on connect and after a mode switch.</summary>
    private void ApplyStatus(JsonElement st)
    {
        if (st.ValueKind != JsonValueKind.Object) return;
        if (st.TryGetProperty("tuner_mode", out var tm))
        {
            _suppressMode = true;
            string mode = tm.GetString() ?? "single";
            SingleRadio.IsChecked = mode == "single";
            DiversityRadio.IsChecked = mode == "diversity";
            _suppressMode = false;
        }
        if (st.TryGetProperty("gain", out var g)) InitGainControls(g);
        AdoptVrx(st);
        ApplyNullStatus(st);
    }

    /// <summary>Null is diversity-only. Enable the bar in diversity, and adopt any
    /// null the server already has engaged (e.g. reconnect, or set via ctl.py).</summary>
    private void ApplyNullStatus(JsonElement st)
    {
        bool diversity = st.TryGetProperty("tuner_mode", out var tm)
                         && tm.GetString() == "diversity";
        NullBar.IsEnabled = diversity;
        if (diversity
            && st.TryGetProperty("combiner", out var c)
            && c.TryGetProperty("type", out var t) && t.GetString() == "null"
            && c.TryGetProperty("active", out var a) && a.GetBoolean())
        {
            _nullActive = true;
            _nullCenterHz = c.GetProperty("center_hz").GetInt64();
            _nullWidthHz = c.GetProperty("width_hz").GetInt64();
            SetNullEngagedUi(true);
            ShowNull(c);
            StartNullPolling();
        }
        else
        {
            ResetNullUi();
        }
    }

    /// <summary>A mode switch drops and re-creates VRXs with fresh ids; adopt the
    /// first one the server restored so the tuned receiver survives the switch.</summary>
    private void AdoptVrx(JsonElement st)
    {
        _vrxId = null;
        if (st.TryGetProperty("vrx", out var arr) && arr.ValueKind == JsonValueKind.Array
            && arr.GetArrayLength() > 0)
        {
            var v = arr[0];
            _vrxId = v.GetProperty("vrx_id").GetInt32();
            _vrxFreq = v.GetProperty("freq_hz").GetInt64();
            _vrxMode = v.GetProperty("mode").GetString() ?? "lsb";
        }
        UpdateMarker();
    }

    private async void OnTunerModeChanged(object? sender, RoutedEventArgs e)
    {
        if (!_connected || _suppressMode || _modeSwitching) return;
        if (sender is not RadioButton rb || rb.IsChecked != true) return;   // act on the checked one only
        string mode = rb == DiversityRadio ? "diversity" : "single";
        _modeSwitching = true;
        ConfigBar.IsEnabled = false;
        StatusText.Text = mode == "diversity"
            ? "Switching to diversity… dual-tuner init can take several seconds."
            : "Switching to single tuner…";
        try
        {
            var st = await Task.Run(() => _client.Send("set_tuner_mode", new { mode }, 20_000));
            ApplyStatus(st);
            StatusText.Text = $"Tuner mode: {st.GetProperty("tuner_mode").GetString()}.";
        }
        catch (Exception ex)
        {
            // diversity init can fail; the server reverts to single. Reflect reality.
            StatusText.Text = "Mode switch failed: " + ex.Message;
            try { ApplyStatus(await Task.Run(() => _client.Send("get_status"))); } catch { /* ignore */ }
        }
        finally { _modeSwitching = false; ConfigBar.IsEnabled = true; }
    }

    private async void OnConnect(object? sender, RoutedEventArgs e)
    {
        if (_connected || _connecting) return;
        _connecting = true;
        ConnectBtn.IsEnabled = false;
        try
        {
            SaveSettings();
            string host = _settings.Host;
            int controlPort = 5555, streamPort = 5556, audioPort = 5557;
            _supervisor = null;

            if (_settings.UseSupervisor)
            {
                var sup = new SupervisorClient(host, _settings.SupervisorPort);
                var st = await LaunchViaSupervisorAsync(sup, host);
                if (st is null) return;                       // status text already explains why
                (controlPort, streamPort, audioPort) = (st.ControlPort, st.StreamPort, st.AudioPort);
                _supervisor = sup;
            }

            StatusText.Text = $"Connecting to {host}…";
            _client.SpectrumReceived += OnSpectrum;
            _client.AudioReceived += OnAudio;

            JsonElement status = default;
            string server = await Task.Run(() =>
            {
                _client.Connect(host, controlPort, streamPort, audioPort);
                var hello = _client.Send("hello", new { protocol_version = "0.1", client = "HfSdr.App/0.1" });
                status = _client.Send("get_status");
                return hello.GetProperty("server").GetString() ?? "?";
            });

            StartAudioOut();
            StartAudioStatsTimer();

            _connected = true;
            ApplyStatus(status);
            ConfigBar.IsEnabled = true;
            StopRxBtn.IsEnabled = _supervisor is not null;
            StatusText.Text = $"Connected to {server} on {host}. Click the waterfall to tune a receiver.";
        }
        catch (Exception ex)
        {
            StatusText.Text = "Connect failed: " + ex.Message;
            _supervisor = null;
            ResetClient();                                    // drop half-open sockets and handlers
        }
        finally
        {
            _connecting = false;
            ConnectBtn.IsEnabled = !_connected;
        }
    }

    /// <summary>
    /// Supervisor launch sequence (design §12.7): status → start if needed → poll until running.
    /// Returns the supervisor's final status, or null after writing the reason to the status line.
    /// </summary>
    private async Task<SupervisorStatus?> LaunchViaSupervisorAsync(SupervisorClient sup, string host)
    {
        StatusText.Text = $"Contacting supervisor on {host}…";
        SupervisorStatus st;
        try { st = await Task.Run(() => sup.Status()); }
        catch (Exception ex)
        {
            StatusText.Text = $"Supervisor not reachable on {host}:{_settings.SupervisorPort} " +
                              $"(service stopped, PC off, or VPN down?) — {ex.Message}";
            return null;
        }

        string mode = DiversityRadio.IsChecked == true ? "diversity" : "single";
        bool requested = false;
        var started = DateTime.UtcNow;
        var deadline = started.AddSeconds(90);                // supervisor's own 20 s start timeout + a diversity switch

        while (true)
        {
            if (st.IsRunning) return st;

            if (st.IsFailed && requested)
            {
                StatusText.Text = DescribeFailure(st);
                return null;
            }
            if (st.State is "stopped" or "failed" && !requested)
            {
                try { await Task.Run(() => sup.Start(_centerHz, mode)); requested = true; }
                catch (Exception ex) { StatusText.Text = "Supervisor refused start: " + ex.Message; return null; }
            }
            if (DateTime.UtcNow > deadline)
            {
                StatusText.Text = "Timed out waiting for the receiver to start.\n" + DescribeFailure(st);
                return null;
            }

            int secs = (int)(DateTime.UtcNow - started).TotalSeconds;
            StatusText.Text = $"Starting receiver on {host} ({st.State}, {secs} s)…";
            await Task.Delay(500);
            try { st = await Task.Run(() => sup.Status()); }
            catch (Exception ex) { StatusText.Text = "Lost contact with supervisor: " + ex.Message; return null; }
        }
    }

    private static string DescribeFailure(SupervisorStatus st)
    {
        var lines = new List<string> { "Receiver failed to start: " + (st.LastError ?? st.State) };
        for (int i = Math.Max(0, st.LogTail.Count - 4); i < st.LogTail.Count; i++) lines.Add("  " + st.LogTail[i]);
        return string.Join("\n", lines);
    }

    private async void OnStopReceiver(object? sender, RoutedEventArgs e)
    {
        if (_supervisor is null) return;
        var sup = _supervisor;
        StopRxBtn.IsEnabled = false;
        StatusText.Text = "Stopping receiver…";
        try
        {
            await Task.Run(() => sup.Stop());
            Disconnect();
            StatusText.Text = "Receiver stopped. Connect to start it again.";
        }
        catch (Exception ex)
        {
            StopRxBtn.IsEnabled = true;
            StatusText.Text = "Stop failed: " + ex.Message;
        }
    }

    /// <summary>Tear the session down to the disconnected state without closing the window.</summary>
    private void Disconnect()
    {
        _nullTimer?.Stop();
        _statsTimer?.Stop();
        _waveOut?.Dispose();
        _waveOut = null;
        ResetClient();
        _connected = false;
        _supervisor = null;
        _vrxId = null;
        _nullActive = false;
        NullMarker.IsVisible = false;
        Marker.IsVisible = false;
        ConfigBar.IsEnabled = false;
        NullBar.IsEnabled = false;
        StopRxBtn.IsEnabled = false;
        ConnectBtn.IsEnabled = true;
    }

    private void ResetClient()
    {
        _client.Dispose();
        _client = new SdrClient();
    }

    private void SaveSettings()
    {
        _settings.Host = string.IsNullOrWhiteSpace(HostBox.Text) ? "localhost" : HostBox.Text.Trim();
        _settings.UseSupervisor = SupervisorBox.IsChecked == true;
        _settings.ReleaseOnClose = ReleaseBox.IsChecked == true;
        _settings.Save();
    }

    private void OnSpectrum(SpectrumFrame f)
    {
        Dispatcher.UIThread.Post(() =>
        {
            _dispCenter = f.CenterHz;
            _dispSpan = f.SpanHz;
            if (_wf is null)
            {
                _wf = new WaterfallRenderer(WaterfallWidth, WaterfallHeight);
                Waterfall.Source = _wf.Bitmap;
            }
            _lastMags = f.Mags;
            _wf.AddRow(f.Mags);
            _wf.Blit();
            Waterfall.InvalidateVisual();
            UpdateMarker();
            UpdateNullMarker();
            ShowPeak(f);
        });
    }

    /// <summary>Peak/overload readout: green with headroom, amber when close,
    /// red "OVERLOAD" at the server's threshold — so a silent overload can't hide.</summary>
    private void ShowPeak(SpectrumFrame f)
    {
        if (double.IsNaN(f.PeakDbfs)) { PeakLabel.Text = "peak —"; return; }
        if (f.Overload)
        {
            PeakLabel.Text = $"⚠ OVERLOAD {f.PeakDbfs:0.0} dBFS";
            PeakLabel.Foreground = Brushes.White;
            PeakBox.Background = Brushes.Firebrick;
        }
        else
        {
            PeakLabel.Text = $"peak {f.PeakDbfs:0.0} dBFS";
            PeakLabel.Foreground = f.PeakDbfs >= -6 ? Brushes.Orange : Brushes.MediumSeaGreen;
            PeakBox.Background = Brushes.Transparent;
        }
    }

    private void OnAudio(short[] samples)
    {
        if (_audioBuf is null) return;
        var bytes = new byte[samples.Length * sizeof(short)];
        Buffer.BlockCopy(samples, 0, bytes, 0, bytes.Length);
        _audioBuf.AddSamples(bytes, 0, bytes.Length);   // thread-safe
        // Bound latency: if the backlog grows past ~400 ms, resync to near-realtime.
        var backlog = _audioBuf.BufferedDuration;
        if (backlog > TimeSpan.FromMilliseconds(400))
        {
            _audioBuf.ClearBuffer();
            _client.Audio.OnResync(backlog.TotalMilliseconds);
        }
    }

    private async void OnTune(object? sender, RoutedEventArgs e)
    {
        if (!_connected) return;
        if (!double.TryParse(FreqBox.Text, out double mhz)) { StatusText.Text = "Bad frequency"; return; }
        _centerHz = (long)(mhz * 1e6);
        try
        {
            await Task.Run(() => _client.Send("set_center_freq", new { hz = _centerHz }));
            StatusText.Text = $"Window centred on {mhz:F3} MHz";
        }
        catch (Exception ex) { StatusText.Text = "Tune failed: " + ex.Message; }
    }

    /// <summary>Map the click X to a frequency in the display window, then place/move the VRX.</summary>
    private async void OnWaterfallClick(object? sender, PointerPressedEventArgs e)
    {
        if (!_connected || _modeSwitching || _dispSpan <= 0) return;
        double w = Waterfall.Bounds.Width;
        if (w <= 0) return;
        double x = e.GetPosition(Waterfall).X;
        long freq = _dispCenter - _dispSpan / 2 + (long)(x / w * _dispSpan);
        // Right-click just records the target; the context menu (below) acts on it.
        if (e.GetCurrentPoint(Waterfall).Properties.IsRightButtonPressed)
        {
            _nullTargetFreq = freq;
            return;
        }
        string mode = ModeBox.SelectedIndex == 1 ? "usb" : "lsb";
        await SetVrx(freq, mode);
    }

    private async Task SetVrx(long freq, string mode)
    {
        if (!_connected || _busy) return;
        _busy = true;
        try
        {
            await Task.Run(() =>
            {
                // mode change can't be updated in place -> remove and re-add
                if (_vrxId != null && mode != _vrxMode)
                {
                    _client.Send("remove_vrx", new { vrx_id = _vrxId.Value });
                    _vrxId = null;
                }
                if (_vrxId == null)
                {
                    var res = _client.Send("add_vrx", new { freq_hz = freq, mode, volume = 0.6 });
                    _vrxId = res.GetProperty("vrx_id").GetInt32();
                    _vrxMode = mode;
                }
                else
                {
                    _client.Send("update_vrx", new { vrx_id = _vrxId.Value, freq_hz = freq });
                }
            });
            _vrxFreq = freq;
            UpdateMarker();
            StatusText.Text = $"VRX @ {freq / 1e6:F4} MHz ({mode.ToUpper()}) — listening";
        }
        catch (Exception ex)
        {
            StatusText.Text = "VRX failed: " + ex.Message;
        }
        finally { _busy = false; }
    }

    private bool _pumping;

    /// <summary>Mouse-wheel fine tuning: 50 Hz/notch, Ctrl = 10 Hz, Shift = 500 Hz.</summary>
    private void OnWaterfallWheel(object? sender, PointerWheelEventArgs e)
    {
        if (!_connected || _modeSwitching || _vrxId is null) return;
        long step = e.KeyModifiers.HasFlag(KeyModifiers.Shift) ? 500
                  : e.KeyModifiers.HasFlag(KeyModifiers.Control) ? 10
                  : 50;
        long delta = e.Delta.Y >= 0 ? step : -step;
        long lo = _dispCenter - 950_000, hi = _dispCenter + 950_000;   // capture window
        _vrxFreq = Math.Clamp(_vrxFreq + delta, lo, hi);
        UpdateMarker();
        StatusText.Text = $"VRX @ {_vrxFreq / 1e6:F4} MHz ({_vrxMode.ToUpper()})";
        PumpVrx();
        e.Handled = true;
    }

    /// <summary>Coalesced sender: keeps the server VRX caught up to _vrxFreq, dropping no notches.</summary>
    private async void PumpVrx()
    {
        if (_pumping) return;
        _pumping = true;
        try
        {
            long sent = long.MinValue;
            while (_connected && _vrxId is int id && _vrxFreq != sent)
            {
                long target = _vrxFreq;
                await Task.Run(() => _client.Send("update_vrx", new { vrx_id = id, freq_hz = target }));
                sent = target;
            }
        }
        catch (Exception ex) { StatusText.Text = "tune failed: " + ex.Message; }
        finally { _pumping = false; }
    }

    /// <summary>Feed the ruler above the waterfall the current display window
    /// (it shares the waterfall's Hz→x mapping) plus the tuned frequency.</summary>
    private void UpdateScale()
    {
        FreqScale.CenterHz = _dispCenter;
        FreqScale.SpanHz = _dispSpan;
        FreqScale.MarkerHz = _vrxId is null ? null : _vrxFreq;
    }

    /// <summary>Position the tuned-frequency marker line over the waterfall.</summary>
    private void UpdateMarker()
    {
        UpdateScale();
        double w = Waterfall.Bounds.Width, h = Waterfall.Bounds.Height;
        if (_vrxId is null || _dispSpan <= 0 || w <= 0)
        {
            Marker.IsVisible = false;
            return;
        }
        double x = (double)(_vrxFreq - (_dispCenter - _dispSpan / 2)) / _dispSpan * w;
        if (x < 0 || x > w) { Marker.IsVisible = false; return; }
        Canvas.SetLeft(Marker, x);
        Canvas.SetTop(Marker, 0);
        Marker.Height = h;
        Marker.IsVisible = true;
    }

    // ---- diversity null (targeted interference canceller) -------------
    private bool _suppressNull;    // guard: programmatic slider/checkbox updates must not send
    private bool _nullTrimDirty;
    private bool _nullTrimPushing;

    /// <summary>Right-click → "Null this source": engage a null on the last
    /// right-clicked frequency, auto-sizing the target band from the spectrum.</summary>
    private async void OnNullThis(object? sender, RoutedEventArgs e)
    {
        if (!_connected) return;
        if (DiversityRadio.IsChecked != true)
        {
            StatusText.Text = "Null needs Diversity mode — switch the tuner to Diversity first.";
            return;
        }
        long width = EstimateNullWidth(_nullTargetFreq);
        try
        {
            var res = await Task.Run(() => _client.Send("null_signal",
                new { center_hz = _nullTargetFreq, width_hz = width, track = true }));
            _nullActive = true;
            _nullCenterHz = _nullTargetFreq;
            _nullWidthHz = width;
            SetNullEngagedUi(true);
            ShowNull(res);
            UpdateNullMarker();
            StartNullPolling();
            StatusText.Text = $"Nulling {_nullTargetFreq / 1e6:F4} MHz (±{width / 2000.0:F1} kHz) — depth climbing…";
        }
        catch (Exception ex) { StatusText.Text = "Null failed: " + ex.Message; }
    }

    private async void OnClearNull(object? sender, RoutedEventArgs e)
    {
        if (!_connected) return;
        try { await Task.Run(() => _client.Send("null_signal", new { clear = true })); }
        catch (Exception ex) { StatusText.Text = "Clear null failed: " + ex.Message; }
        ResetNullUi();
        StatusText.Text = "Null cleared — back to MRC diversity.";
    }

    private void OnNullTrackChanged(object? sender, RoutedEventArgs e)
    {
        if (!_connected || _suppressNull || !_nullActive) return;
        bool track = NullTrackBox.IsChecked == true;
        _ = SendNull(new { track });
    }

    private void OnNullSpeedChanged(object? sender, SelectionChangedEventArgs e)
    {
        if (!_connected || _suppressNull || !_nullActive) return;
        string speed = NullSpeedBox.SelectedIndex switch { 0 => "fast", 2 => "slow", _ => "med" };
        _ = SendNull(new { track_speed = speed });
    }

    private void OnNullTrimChanged(object? sender, Avalonia.Controls.Primitives.RangeBaseValueChangedEventArgs e)
    {
        if (!_connected || _suppressNull || !_nullActive) return;
        _nullTrimDirty = true;
        PushNullTrim();
    }

    /// <summary>Coalesced manual-trim sender: pushes the latest amp/phase, never
    /// overlapping requests. Manual trim switches the server out of tracking.</summary>
    private async void PushNullTrim()
    {
        if (_nullTrimPushing) return;
        _nullTrimPushing = true;
        try
        {
            while (_connected && _nullActive && _nullTrimDirty)
            {
                _nullTrimDirty = false;
                double amp = NullAmpSlider.Value;
                double phase = NullPhaseSlider.Value;
                var res = await Task.Run(() => _client.Send("null_signal",
                    new { amp, phase_deg = phase }));
                ShowNull(res);
            }
        }
        catch (Exception ex) { StatusText.Text = "null trim failed: " + ex.Message; }
        finally { _nullTrimPushing = false; }
    }

    private async Task SendNull(object p)
    {
        try { ShowNull(await Task.Run(() => _client.Send("null_signal", p))); }
        catch (Exception ex) { StatusText.Text = "null failed: " + ex.Message; }
    }

    /// <summary>Auto-size the null band: find the local peak near the click and
    /// take its −10 dB width from the latest spectrum. Falls back to 12 kHz.</summary>
    private long EstimateNullWidth(long freq)
    {
        const long fallback = 12_000;
        var mags = _lastMags;
        if (mags is null || mags.Length < 8 || _dispSpan <= 0) return fallback;
        int n = mags.Length;
        double hzPerBin = (double)_dispSpan / n;
        long left = _dispCenter - _dispSpan / 2;
        int click = (int)Math.Clamp((freq - left) / hzPerBin, 0, n - 1);
        int span = Math.Max(1, (int)(4000 / hzPerBin));      // snap to the peak within ±4 kHz
        int peak = click;
        for (int i = Math.Max(0, click - span); i <= Math.Min(n - 1, click + span); i++)
            if (mags[i] > mags[peak]) peak = i;
        double thresh = mags[peak] - 10.0;
        int lo = peak, hi = peak;
        while (lo > 0 && mags[lo - 1] >= thresh) lo--;
        while (hi < n - 1 && mags[hi + 1] >= thresh) hi++;
        long width = (long)((hi - lo + 1) * hzPerBin);
        return Math.Clamp(width, 3_000, 40_000);
    }

    private void StartNullPolling()
    {
        _nullTimer ??= new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(400) };
        _nullTimer.Tick -= NullPollTick;
        _nullTimer.Tick += NullPollTick;
        _nullTimer.Start();
    }

    private async void NullPollTick(object? sender, EventArgs e)
    {
        if (!_connected || !_nullActive) { _nullTimer?.Stop(); return; }
        try
        {
            var st = await Task.Run(() => _client.Send("get_status"));
            if (st.TryGetProperty("combiner", out var c)
                && c.TryGetProperty("type", out var t) && t.GetString() == "null")
                ShowNull(c);
        }
        catch { /* transient; next tick retries */ }
    }

    /// <summary>Reflect a combiner-null object (from a reply or status) in the readout,
    /// seeding the trim controls without triggering sends.</summary>
    private void ShowNull(JsonElement c)
    {
        if (c.ValueKind != JsonValueKind.Object) return;
        double depth = c.TryGetProperty("null_depth_db", out var d) ? d.GetDouble() : 0;
        double amp = c.TryGetProperty("amp", out var a) ? a.GetDouble() : 0;
        double phase = c.TryGetProperty("phase_deg", out var p) ? p.GetDouble() : 0;
        bool track = !c.TryGetProperty("track", out var tr) || tr.GetBoolean();
        bool manual = c.TryGetProperty("manual", out var m) && m.GetBoolean();

        _suppressNull = true;
        NullAmpSlider.Value = Math.Clamp(amp, NullAmpSlider.Minimum, NullAmpSlider.Maximum);
        NullPhaseSlider.Value = Math.Clamp(phase, NullPhaseSlider.Minimum, NullPhaseSlider.Maximum);
        NullTrackBox.IsChecked = track;
        if (c.TryGetProperty("track_speed", out var sp))
            NullSpeedBox.SelectedIndex = sp.GetString() switch { "fast" => 0, "slow" => 2, _ => 1 };
        _suppressNull = false;

        string state = manual ? "manual" : track ? "tracking" : "frozen";
        NullStatusLabel.Text = $"−{depth:0.0} dB @ {_nullCenterHz / 1e6:F4} ({state})";
        // green once it's biting, amber while it settles
        NullStatusLabel.Foreground = depth >= 15 ? Brushes.MediumSeaGreen
                                    : depth >= 6 ? Brushes.Orange : Brushes.Gray;
    }

    private void SetNullEngagedUi(bool engaged)
    {
        NullTrackBox.IsEnabled = engaged;
        NullSpeedBox.IsEnabled = engaged;
        NullAmpSlider.IsEnabled = engaged;
        NullPhaseSlider.IsEnabled = engaged;
        NullClearBtn.IsEnabled = engaged;
    }

    private void ResetNullUi()
    {
        _nullActive = false;
        _nullTimer?.Stop();
        SetNullEngagedUi(false);
        NullMarker.IsVisible = false;
        NullStatusLabel.Text = "off — right-click a signal to null it";
        NullStatusLabel.Foreground = Brushes.Gray;
    }

    /// <summary>Shade the nulled band across the waterfall so the target is visible.</summary>
    private void UpdateNullMarker()
    {
        double w = Waterfall.Bounds.Width, h = Waterfall.Bounds.Height;
        if (!_nullActive || _dispSpan <= 0 || w <= 0) { NullMarker.IsVisible = false; return; }
        double left = (double)(_nullCenterHz - _nullWidthHz / 2 - (_dispCenter - _dispSpan / 2)) / _dispSpan * w;
        double width = (double)_nullWidthHz / _dispSpan * w;
        double x0 = Math.Max(0, left), x1 = Math.Min(w, left + width);
        if (x1 <= 0 || x0 >= w) { NullMarker.IsVisible = false; return; }
        Canvas.SetLeft(NullMarker, x0);
        Canvas.SetTop(NullMarker, 0);
        NullMarker.Width = Math.Max(1, x1 - x0);
        NullMarker.Height = h;
        NullMarker.IsVisible = true;
    }

    protected override void OnClosed(EventArgs e)
    {
        SaveSettings();
        // Opt-in only: the remote PC may be serving another client, so by default leave the receiver running.
        if (_connected && _supervisor is not null && _settings.ReleaseOnClose)
        {
            try { _supervisor.Stop(2000); } catch { /* best effort */ }
        }
        _nullTimer?.Stop();
        _statsTimer?.Stop();
        _waveOut?.Dispose();
        _client.Dispose();
        base.OnClosed(e);
    }
}
