using System;
using System.Runtime.InteropServices;
using Avalonia;
using Avalonia.Media.Imaging;
using Avalonia.Platform;

namespace HfSdr.App;

/// <summary>
/// A simple scrolling waterfall backed by a BGRA pixel buffer. Newest line at
/// the top; older lines scroll down. FFT bins are downsampled to the bitmap
/// width. dBFS is mapped through a jet-style colour map.
/// </summary>
public sealed class WaterfallRenderer
{
    private readonly int _w;
    private readonly int _h;
    private readonly byte[] _px;           // BGRA, _w*_h*4

    public WriteableBitmap Bitmap { get; }
    public double MinDb { get; set; } = -110;
    public double MaxDb { get; set; } = -20;

    public WaterfallRenderer(int width, int height)
    {
        _w = width;
        _h = height;
        _px = new byte[_w * _h * 4];
        Bitmap = new WriteableBitmap(new PixelSize(_w, _h), new Vector(96, 96),
                                     PixelFormat.Bgra8888, AlphaFormat.Premul);
    }

    public void AddRow(float[] mags)
    {
        // scroll everything down one row
        Array.Copy(_px, 0, _px, _w * 4, (_h - 1) * _w * 4);

        int n = mags.Length;
        for (int x = 0; x < _w; x++)
        {
            int bin = n == _w ? x : (int)((long)x * n / _w);
            var (b, g, r) = Colour(mags[bin]);
            int o = x * 4;
            _px[o] = b; _px[o + 1] = g; _px[o + 2] = r; _px[o + 3] = 255;
        }
    }

    /// <summary>Copy the pixel buffer into the bitmap (call on the UI thread).</summary>
    public void Blit()
    {
        using var fb = Bitmap.Lock();
        int rowLen = _w * 4;
        for (int y = 0; y < _h; y++)
            Marshal.Copy(_px, y * rowLen, IntPtr.Add(fb.Address, y * fb.RowBytes), rowLen);
    }

    private (byte b, byte g, byte r) Colour(double db)
    {
        double t = Math.Clamp((db - MinDb) / (MaxDb - MinDb), 0, 1);
        double r = Math.Clamp(1.5 - Math.Abs(4 * t - 3), 0, 1);
        double g = Math.Clamp(1.5 - Math.Abs(4 * t - 2), 0, 1);
        double b = Math.Clamp(1.5 - Math.Abs(4 * t - 1), 0, 1);
        return ((byte)(b * 255), (byte)(g * 255), (byte)(r * 255));
    }
}
