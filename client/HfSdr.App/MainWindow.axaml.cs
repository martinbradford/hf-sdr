using System;
using System.Threading.Tasks;
using Avalonia.Controls;
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
    private long _centerHz = 7_150_000;

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
                var hello = _client.Send("hello",
                    new { protocol_version = "0.1", client = "HfSdr.App/0.1" });
                _ = _client.Send("get_status");
                return hello.GetProperty("server").GetString() ?? "?";
            });

            // audio output: 48 kHz / 16-bit / mono, fed as int16 arrives
            _audioBuf = new BufferedWaveProvider(new WaveFormat(48_000, 16, 1))
            {
                BufferDuration = TimeSpan.FromSeconds(4),
                DiscardOnBufferOverflow = true
            };
            _waveOut = new WaveOutEvent();
            _waveOut.Init(_audioBuf);
            _waveOut.Play();

            _connected = true;
            StatusText.Text = $"Connected to {server}. Waterfall live — 'Add VRX' to hear audio.";
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
            if (_wf is null)
            {
                _wf = new WaterfallRenderer(WaterfallWidth, WaterfallHeight);
                Waterfall.Source = _wf.Bitmap;
            }
            _wf.AddRow(f.Mags);
            _wf.Blit();
            Waterfall.InvalidateVisual();
        });
    }

    private void OnAudio(short[] samples)
    {
        if (_audioBuf is null) return;
        var bytes = new byte[samples.Length * sizeof(short)];
        Buffer.BlockCopy(samples, 0, bytes, 0, bytes.Length);
        _audioBuf.AddSamples(bytes, 0, bytes.Length);   // AddSamples is thread-safe
    }

    private async void OnTune(object? sender, RoutedEventArgs e)
    {
        if (!_connected) return;
        if (!double.TryParse(FreqBox.Text, out double mhz)) { StatusText.Text = "Bad frequency"; return; }
        _centerHz = (long)(mhz * 1e6);
        await Control("set_center_freq", new { hz = _centerHz }, $"Tuned {mhz:F3} MHz");
    }

    private async void OnAddVrx(object? sender, RoutedEventArgs e)
    {
        if (!_connected) return;
        await Control("add_vrx", new { freq_hz = _centerHz, mode = "lsb", volume = 0.6 },
                      "VRX added — listening");
    }

    private async Task Control(string cmd, object p, string okMsg)
    {
        try
        {
            await Task.Run(() => _client.Send(cmd, p));
            StatusText.Text = okMsg;
        }
        catch (Exception ex)
        {
            StatusText.Text = $"{cmd} failed: {ex.Message}";
        }
    }

    protected override void OnClosed(EventArgs e)
    {
        _waveOut?.Dispose();
        _client.Dispose();
        base.OnClosed(e);
    }
}
