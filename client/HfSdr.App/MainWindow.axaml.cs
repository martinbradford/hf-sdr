using System;
using System.Threading.Tasks;
using Avalonia.Controls;
using Avalonia.Input;
using Avalonia.Interactivity;
using Avalonia.Threading;
using NAudio.Wave;

namespace HfSdr.App;

public partial class MainWindow : Window
{
    private readonly SdrClient _client = new();
    private WaterfallRenderer? _wf;
    private WaveOutEvent? _waveOut;
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

    public MainWindow() => InitializeComponent();

    private async void OnConnect(object? sender, RoutedEventArgs e)
    {
        if (_connected) return;
        try
        {
            _client.SpectrumReceived += OnSpectrum;
            _client.AudioReceived += OnAudio;

            string server = await Task.Run(() =>
            {
                _client.Connect();
                var hello = _client.Send("hello", new { protocol_version = "0.1", client = "HfSdr.App/0.1" });
                _ = _client.Send("get_status");
                return hello.GetProperty("server").GetString() ?? "?";
            });

            _audioBuf = new BufferedWaveProvider(new WaveFormat(48_000, 16, 1))
            {
                BufferDuration = TimeSpan.FromSeconds(4),
                DiscardOnBufferOverflow = true
            };
            _waveOut = new WaveOutEvent();
            _waveOut.Init(_audioBuf);
            _waveOut.Play();

            _connected = true;
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
        });
    }

    private void OnAudio(short[] samples)
    {
        if (_audioBuf is null) return;
        var bytes = new byte[samples.Length * sizeof(short)];
        Buffer.BlockCopy(samples, 0, bytes, 0, bytes.Length);
        _audioBuf.AddSamples(bytes, 0, bytes.Length);   // thread-safe
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
        if (!_connected || _dispSpan <= 0) return;
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
