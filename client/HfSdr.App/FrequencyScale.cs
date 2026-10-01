using System;
using System.Globalization;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Media;

namespace HfSdr.App;

/// <summary>
/// Frequency ruler drawn above the waterfall. It uses the same linear Hz→x
/// mapping as the waterfall (the Image is Stretch="Fill", so x/width maps
/// straight onto the display span), which keeps the ticks aligned with the
/// bins underneath them. Tick spacing is chosen from the live span so the
/// labels never collide: a 1/2/5 × 10ⁿ step, with minor ticks between.
/// </summary>
public sealed class FrequencyScale : Control
{
    public static readonly StyledProperty<long> CenterHzProperty =
        AvaloniaProperty.Register<FrequencyScale, long>(nameof(CenterHz));

    /// <summary>Width of the displayed window in Hz. 0 = nothing to draw yet.</summary>
    public static readonly StyledProperty<long> SpanHzProperty =
        AvaloniaProperty.Register<FrequencyScale, long>(nameof(SpanHz));

    /// <summary>Tuned VRX frequency, marked with a caret. Null = no VRX.</summary>
    public static readonly StyledProperty<long?> MarkerHzProperty =
        AvaloniaProperty.Register<FrequencyScale, long?>(nameof(MarkerHz));

    static FrequencyScale()
    {
        AffectsRender<FrequencyScale>(CenterHzProperty, SpanHzProperty, MarkerHzProperty);
    }

    public long CenterHz
    {
        get => GetValue(CenterHzProperty);
        set => SetValue(CenterHzProperty, value);
    }

    public long SpanHz
    {
        get => GetValue(SpanHzProperty);
        set => SetValue(SpanHzProperty, value);
    }

    public long? MarkerHz
    {
        get => GetValue(MarkerHzProperty);
        set => SetValue(MarkerHzProperty, value);
    }

    private const double FontSize = 11;
    private const double MinLabelGapPx = 68;      // keeps "7.1500"-sized labels apart
    private const double MajorTickLen = 9;
    private const double MinorTickLen = 5;

    private static readonly IPen Axis = new Pen(new SolidColorBrush(Color.FromRgb(0x66, 0x66, 0x66)), 1);
    private static readonly IPen MajorPen = new Pen(new SolidColorBrush(Color.FromRgb(0xB0, 0xB0, 0xB0)), 1);
    private static readonly IPen MinorPen = new Pen(new SolidColorBrush(Color.FromRgb(0x70, 0x70, 0x70)), 1);
    private static readonly IBrush LabelBrush = new SolidColorBrush(Color.FromRgb(0xD0, 0xD0, 0xD0));
    private static readonly IBrush MarkerBrush = new SolidColorBrush(Color.FromRgb(0xFF, 0xEB, 0x3B));
    private static readonly Typeface Face = new("Consolas");

    public override void Render(DrawingContext ctx)
    {
        double w = Bounds.Width, h = Bounds.Height;
        if (w <= 0 || h <= 0) return;

        // Baseline sits on the waterfall's top edge, so it reads as one widget.
        double baseY = h - 0.5;
        ctx.DrawLine(Axis, new Point(0, baseY), new Point(w, baseY));

        long span = SpanHz;
        if (span <= 0) return;

        long left = CenterHz - span / 2;
        double pxPerHz = w / span;

        double step = NiceStep(span / Math.Max(1.0, w / MinLabelGapPx));
        int minors = MinorsPerMajor(step);
        int decimals = MhzDecimals(step);

        // First major at or below the left edge, so partial labels still anchor.
        double first = Math.Floor(left / step) * step;

        for (double f = first; f <= left + span + step; f += step)
        {
            for (int m = 1; m < minors; m++)
            {
                double fm = f + step * m / minors;
                double xm = (fm - left) * pxPerHz;
                if (xm >= 0 && xm <= w)
                    ctx.DrawLine(MinorPen, new Point(Snap(xm), h - MinorTickLen), new Point(Snap(xm), h));
            }

            double x = (f - left) * pxPerHz;
            if (x < 0 || x > w) continue;
            ctx.DrawLine(MajorPen, new Point(Snap(x), h - MajorTickLen), new Point(Snap(x), h));

            var label = new FormattedText(
                (f / 1e6).ToString("F" + decimals, CultureInfo.InvariantCulture),
                CultureInfo.InvariantCulture, FlowDirection.LeftToRight,
                Face, FontSize, LabelBrush);
            // Nudge edge labels inside the control rather than clipping them.
            double tx = Math.Clamp(x - label.Width / 2, 0, Math.Max(0, w - label.Width));
            ctx.DrawText(label, new Point(tx, Math.Max(0, h - MajorTickLen - label.Height - 1)));
        }

        if (MarkerHz is long mk)
        {
            double x = (mk - left) * pxPerHz;
            if (x >= 0 && x <= w)
            {
                // Downward caret pointing at the tuned frequency.
                var caret = new PathGeometry();
                using (var g = caret.Open())
                {
                    g.BeginFigure(new Point(x - 4, h - 7), true);
                    g.LineTo(new Point(x + 4, h - 7));
                    g.LineTo(new Point(x, h - 1));
                    g.EndFigure(true);
                }
                ctx.DrawGeometry(MarkerBrush, null, caret);
            }
        }
    }

    /// <summary>Round a raw step up to the next 1/2/5 × 10ⁿ.</summary>
    private static double NiceStep(double raw)
    {
        if (raw <= 0 || double.IsNaN(raw)) return 1;
        double decade = Math.Pow(10, Math.Floor(Math.Log10(raw)));
        double mult = raw / decade;
        double nice = mult <= 1 ? 1 : mult <= 2 ? 2 : mult <= 5 ? 5 : 10;
        return nice * decade;
    }

    /// <summary>Subdivisions of a major interval: 2-steps split in 4, others in 5.</summary>
    private static int MinorsPerMajor(double step)
    {
        double decade = Math.Pow(10, Math.Floor(Math.Log10(step)));
        return Math.Abs(step / decade - 2) < 1e-9 ? 4 : 5;
    }

    /// <summary>Just enough decimals in MHz to keep neighbouring labels distinct.</summary>
    private static int MhzDecimals(double stepHz)
    {
        double stepMhz = stepHz / 1e6;
        if (stepMhz <= 0) return 3;
        return Math.Clamp((int)Math.Ceiling(-Math.Log10(stepMhz)), 0, 6);
    }

    /// <summary>Half-pixel alignment so 1px ticks render crisp, not blurred over two columns.</summary>
    private static double Snap(double x) => Math.Floor(x) + 0.5;
}
