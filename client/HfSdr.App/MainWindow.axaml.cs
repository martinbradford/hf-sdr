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
    private readonly SdrClient _client = new();
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

    private const int WaterfallWidth = 1024;
    private const int WaterfallHeight = 320;

    public MainWindow()
    {
        InitializeComponent();
        PopulateAudioDevices();
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
        _waveOut.Init(_audioBuf);
        _waveOut.Play();
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

    private static int GainInt(JsonElement g, string name, int fallback) =>
        g.TryGetProperty(name, out var v) ? v.GetInt32() : fallback;

    /// <summary>Seed the gain UI from a server status "gain" block (ranges + values).</summary>
    private void InitGainControls(JsonElement g)
    {
        _suppressGain = true;
        if (g.TryGetProperty("rf_gr_db_range", out var rr) && rr.GetArrayLength() == 2)
        {
            RfSlider.Minimum = rr[0].GetDouble();
            RfSlider.Maximum = rr[1].GetDouble();
        }
        if (g.TryGetProperty("if_gr_db_range", out var ir) && ir.GetArrayLength() == 2)
        {
            IfSlider.Minimum = ir[0].GetDouble();
            IfSlider.Maximum = ir[1].GetDouble();
        }
        RfSlider.Value = GainInt(g, "rf_gr_db", 0);
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
        RfSlider.Value = rf;                       // show the value the driver snapped to
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
                int rf = (int)Math.Round(RfSlider.Value);
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
        if (_connected) return;
        try
        {
            _client.SpectrumReceived += OnSpectrum;
            _client.AudioReceived += OnAudio;

            JsonElement status = default;
            string server = await Task.Run(() =>
            {
                _client.Connect();
                var hello = _client.Send("hello", new { protocol_version = "0.1", client = "HfSdr.App/0.1" });
                status = _client.Send("get_status");
                return hello.GetProperty("server").GetString() ?? "?";
            });

            StartAudioOut();

            _connected = true;
            ApplyStatus(status);
            ConfigBar.IsEnabled = true;
            StatusText.Text = $"Connected to {server}. Click the waterfall to tune a receiver.";
        }
        catch (Exception ex)
        {
            StatusText.Text = "Connect failed: " + ex.Message;
        }
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
            _wf.AddRow(f.Mags);
            _wf.Blit();
            Waterfall.InvalidateVisual();
            UpdateMarker();
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
        if (_audioBuf.BufferedDuration > TimeSpan.FromMilliseconds(400))
            _audioBuf.ClearBuffer();
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

    /// <summary>Position the tuned-frequency marker line over the waterfall.</summary>
    private void UpdateMarker()
    {
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

    protected override void OnClosed(EventArgs e)
    {
        _waveOut?.Dispose();
        _client.Dispose();
        base.OnClosed(e);
    }
}
