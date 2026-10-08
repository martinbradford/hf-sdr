namespace HfSdr.App;

/// <summary>
/// Audio passband of a sideband receiver, expressed the way the server wants it: filter edges in Hz
/// relative to the tuned (carrier) frequency. The UI chooses a width; the low edge is fixed at the
/// server's default voice value (200 Hz) so the default width (2.8 kHz) reproduces the default
/// passband exactly: USB 200..3000 Hz, LSB -3000..-200 Hz.
/// </summary>
internal static class Passband
{
    public const int LowEdgeHz = 200;
    public const int DefaultWidthHz = 2800;

    /// <summary>Filter edges (low, high) for a mode and audio bandwidth. LSB is the mirror image of USB.</summary>
    public static (int Low, int High) Edges(string mode, int widthHz) =>
        mode == "usb"
            ? (LowEdgeHz, LowEdgeHz + widthHz)
            : (-(LowEdgeHz + widthHz), -LowEdgeHz);
}
