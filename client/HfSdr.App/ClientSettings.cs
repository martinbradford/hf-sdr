using System;
using System.IO;
using System.Text.Json;

namespace HfSdr.App;

/// <summary>Connection settings, persisted to %APPDATA%\HfSdr\settings.json.</summary>
public sealed class ClientSettings
{
    /// <summary>PC with the RSP ("localhost" for a single-PC setup).</summary>
    public string Host { get; set; } = "localhost";
    /// <summary>Ask the supervisor service on <see cref="Host"/> to launch the server (design §12.7).
    /// Off = attach to a server that is already running.</summary>
    public bool UseSupervisor { get; set; }
    public int SupervisorPort { get; set; } = 5554;
    /// <summary>Opt-in: stop the remote receiver when the window closes. Off by default because another
    /// client may be using it, or the user may want it left running (design §12.7).</summary>
    public bool ReleaseOnClose { get; set; }
    /// <summary>Playback jitter cushion in ms (0-300): audio is held until this much is buffered, trading
    /// that much latency for resilience to network jitter. 0 disables it. Edit settings.json to tune.</summary>
    public int AudioPrimeMs { get; set; } = 80;

    private static string PathFile => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "HfSdr", "settings.json");

    public static ClientSettings Load()
    {
        try { return JsonSerializer.Deserialize<ClientSettings>(File.ReadAllText(PathFile)) ?? new(); }
        catch { return new(); }          // missing or corrupt: defaults
    }

    public void Save()
    {
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(PathFile)!);
            File.WriteAllText(PathFile, JsonSerializer.Serialize(this, new JsonSerializerOptions { WriteIndented = true }));
        }
        catch { /* settings are a convenience; never fail the app over them */ }
    }
}
